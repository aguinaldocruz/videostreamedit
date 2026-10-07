"""Exercise one-media cache work and priority ordering without production DB."""

import ast
import hashlib
import json
import sqlite3
import sys
import types
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

sys.modules.setdefault("app", types.ModuleType("app"))
layout_module = types.ModuleType("app.matroska_layout")
layout_checks = []
layout_module.safe_checkpoint = lambda media: layout_checks.append(str(media))
sys.modules["app.matroska_layout"] = layout_module


def extract_names(file: str, names: set[str], scope: dict) -> None:
    tree = ast.parse((Path(__file__).resolve().parents[1] / file).read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    for node in nodes:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, "exec"), scope)


database = sqlite3.connect(":memory:")
database.row_factory = sqlite3.Row
database.execute("PRAGMA foreign_keys=ON")


@contextmanager
def connection():
    with database:
        yield database


scope = {"connect": connection, "hashlib": hashlib, "json": json, "dataclass": dataclass, "Callable": Callable,
         "CACHE_FORMAT_VERSION": 1, "IMAGE_CACHE_FORMAT_VERSION": 1,
         "Path": Path, "MAX_TEXT_BYTES": 32 * 1024 * 1024,
         "TEXT_CODECS": {"subrip"}, "TEXT_SIDECAR_SUFFIXES": {".srt"}}
extract_names("app/subtitle_cache.py", {"TextSubtitle", "ensure_subtitle_cache_schema", "invalidate_media", "enqueue_media", "enqueue_media_many", "cache_record_complete", "pending_revision", "complete_pending_media", "publish_media", "publish_track", "publish_replacement_cache", "get_valid_media", "get_valid_track", "manifest_complete"}, scope)
extract_names("app/subtitle_cache_worker.py", {"_file_identity", "text_track_manifest", "_indexed_text_track_presence", "cache_media", "cached_or_extract_track", "ordered_catalog_candidates"}, scope)
extract_names("app/subtitle_cache_worker.py", {"cache_status"}, scope)
scope["ensure_subtitle_cache_schema"]()
scope["image_cache_work_required"] = lambda _path: False
scope["image_track_manifest"] = lambda _media, _metadata=None: ("empty-image-manifest", [])

with TemporaryDirectory() as temp:
    media = Path(temp) / "episode.mkv"
    media.write_bytes(b"media-data")
    sidecar = Path(temp) / "episode.pt.srt"
    sidecar.write_text("original")
    metadata = {"streams": [{"codec_type": "subtitle", "codec_name": "subrip"}]}
    scope["probe"] = lambda _media: metadata
    scope["external_subtitles"] = lambda _media: [{"path": sidecar}]
    calls = []

    def extract(_media, track, _timeout=300, _low_priority=False):
        calls.append(track["source"])
        return scope["TextSubtitle"](track["source"], track["type_index"], track["external_path"], track["codec"],
                                      "1\n00:00:00,000 --> 00:00:01,000\nHello\n")

    scope["_extract_track"] = extract
    assert scope["cache_status"](media, metadata)["status"] == "not_cached"
    selected = scope["cached_or_extract_track"](media, "embedded", 0, metadata=metadata)
    assert selected.source == "embedded" and calls == ["embedded"]
    assert layout_checks == [str(media)]
    assert scope["cache_status"](media, metadata)["status"] == "partial"
    assert scope["cache_status"](media, metadata)["status"] == "partial"
    assert scope["cached_or_extract_track"](media, "embedded", 0, metadata=metadata) == selected
    assert layout_checks == [str(media), str(media)]
    assert calls == ["embedded"]
    assert scope["cache_media"](media, metadata)["status"] == "extracted"
    assert calls == ["embedded", "external"]
    assert scope["cache_media"](media, metadata)["status"] == "cached"
    assert scope["cache_status"](media, metadata)["status"] == "cached"
    assert calls == ["embedded", "external"]
    scope["probe"] = lambda _media: (_ for _ in ()).throw(AssertionError("Complete cache must not probe media"))
    assert scope["cache_media"](media)["status"] == "cached"
    scope["probe"] = lambda _media: metadata
    sidecar.write_text("changed")
    assert scope["cache_status"](media, metadata)["status"] == "needs_refresh"
    scope["invalidate_media"](str(media))  # The sidecar index owns invalidation.
    assert scope["cache_status"](media, metadata)["status"] == "not_cached"
    signature, tracks = scope["text_track_manifest"](media, metadata)
    scope["publish_replacement_cache"](str(media), signature, {(t['source'], t['type_index'], t['external_path']) for t in tracks},
        [scope['TextSubtitle']('embedded', 0, '', 'subrip', "1\n00:00:00,000 --> 00:00:01,000\nEdited\n")],
        image_before_signature='', image_after_signature='', expected_images=set())
    scope['enqueue_media'](str(media))  # The caller queues only genuinely missing tracks.
    assert scope["cache_status"](media, metadata)["status"] == "partial"
    assert scope["pending_revision"](str(media)) is not None
    signature, _ = scope["text_track_manifest"](media, metadata)
    assert scope["get_valid_track"](str(media), signature, ("embedded", 0, "")).text.endswith("Edited\n")
    assert scope["get_valid_track"](str(media), signature, ("external", -1, str(sidecar))) is None
    assert scope["cache_media"](media, metadata)["status"] == "extracted"
    assert scope["pending_revision"](str(media)) is None
    assert calls == ["embedded", "external", "external"]
    media.write_bytes(b"new-media-data")
    assert scope["cache_status"](media, metadata)["status"] == "needs_refresh"
    try:
        scope["cache_media"](media, metadata, remaining_seconds=lambda: 0)
    except InterruptedError:
        pass
    else:
        raise AssertionError("Time-limited cache run published after its deadline")
    assert scope["cache_status"](media, metadata)["status"] == "needs_refresh"

database.executescript("""
CREATE TABLE plex_media(path TEXT,kind TEXT,library_key TEXT,show_title TEXT,plex_added_at INTEGER);
CREATE TABLE media_notes(entity_type TEXT,entity_key TEXT,final_version INTEGER);
INSERT INTO plex_media VALUES('/old-final','movie','','',10);
INSERT INTO plex_media VALUES('/new-final','movie','','',20);
INSERT INTO plex_media VALUES('/new-normal','movie','','',30);
INSERT INTO media_notes VALUES('movie','/old-final',1);
INSERT INTO media_notes VALUES('movie','/new-final',1);
""")
assert scope["ordered_catalog_candidates"]() == ["/new-final", "/old-final", "/new-normal"]
database.executescript("""
CREATE TABLE media_stream_index_state(path TEXT PRIMARY KEY,modified_ns INTEGER,size INTEGER);
CREATE TABLE media_stream_index(path TEXT,source TEXT,stream_type TEXT,codec TEXT);
""")
with TemporaryDirectory() as temp:
    no_subtitles = Path(temp) / "audio-only.mkv"
    no_subtitles.write_bytes(b"audio-only")
    stat = no_subtitles.stat()
    database.execute("INSERT INTO media_stream_index_state VALUES(?,?,?)", (str(no_subtitles), stat.st_mtime_ns, stat.st_size))
    scope["external_subtitles"] = lambda _media: []
    scope["probe"] = lambda _media: (_ for _ in ()).throw(AssertionError("Unneeded probe"))
    assert scope["cache_media"](no_subtitles)["status"] == "no_subtitles"
    assert scope["pending_revision"](str(no_subtitles)) is None
    sidecar = no_subtitles.with_name("audio-only.pt.srt")
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nOlá\n")
    scope["external_subtitles"] = lambda _media: [{"path": str(sidecar)}]
    assert scope["_indexed_text_track_presence"](no_subtitles) is True
    no_subtitles.write_bytes(b"changed")
    assert scope["_indexed_text_track_presence"](no_subtitles) is True
with TemporaryDirectory() as temp:
    multi = Path(temp) / "two-subtitles.mkv"
    multi.write_bytes(b"two-subtitle-test-media")
    scope["probe"] = lambda _media: {"streams": [{"codec_type": "subtitle", "codec_name": "subrip"}] * 2}
    scope["external_subtitles"] = lambda _media: []
    batch_calls = []

    def batch(_media, tracks, _remaining=300, _low_priority=False):
        batch_calls.append([track["type_index"] for track in tracks])
        return {track["type_index"]: scope["TextSubtitle"]("embedded", track["type_index"], "", "subrip",
                                                             f"1\n00:00:00,000 --> 00:00:01,000\nTrack {track['type_index']}\n")
                for track in tracks}

    scope["_extract_embedded_batch"] = batch
    scope["_extract_track"] = lambda *_: (_ for _ in ()).throw(AssertionError("Batch should cover both tracks"))
    assert scope["cache_media"](multi)["status"] == "extracted"
    assert batch_calls == [[0, 1]]
    signature, _ = scope["text_track_manifest"](multi)
    assert len(scope["get_valid_media"](str(multi), signature)) == 2

    fallback = Path(temp) / "fallback.mkv"
    fallback.write_bytes(b"fallback-test-media")
    scope["_extract_embedded_batch"] = lambda *_: {}
    per_track = []

    def isolated(_media, track, *_):
        per_track.append(track["type_index"])
        return scope["TextSubtitle"]("embedded", track["type_index"], "", "subrip",
                                      f"1\n00:00:00,000 --> 00:00:01,000\nTrack {track['type_index']}\n")

    scope["_extract_track"] = isolated
    assert scope["cache_media"](fallback)["status"] == "extracted"
    assert per_track == [0, 1]
print("PASS: selected-track read-through, full-media completion, sidecar invalidation, final/newest ordering")
