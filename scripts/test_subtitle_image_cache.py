"""Exercise image-only, mixed, and text-only cache work without production DB."""

import ast
from collections.abc import Callable
import hashlib
import json
import sqlite3
import sys
import types
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


root = Path(__file__).resolve().parents[1]
sys.modules.setdefault("app", types.ModuleType("app"))
layout = types.ModuleType("app.matroska_layout")
layout.safe_checkpoint = lambda _media: None
sys.modules["app.matroska_layout"] = layout


def load(file, names, scope):
    tree = ast.parse((root / file).read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    for node in nodes:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), file, "exec"), scope)


database = sqlite3.connect(":memory:")
database.row_factory = sqlite3.Row
database.execute("PRAGMA foreign_keys=ON")


@contextmanager
def connect():
    with database:
        yield database


scope = {
    "connect": connect, "dataclass": dataclass, "hashlib": hashlib, "json": json, "Path": Path, "Callable": Callable,
    "CACHE_FORMAT_VERSION": 1, "IMAGE_CACHE_FORMAT_VERSION": 1,
    "IMAGE_CODECS": {"hdmv_pgs_subtitle": "pgs", "dvd_subtitle": "vobsub"},
    "MAX_IMAGE_BYTES": 256 * 1024 * 1024, "MAX_IMAGE_INDEX_BYTES": 4 * 1024 * 1024,
}
load("app/subtitle_cache.py", {"TextSubtitle", "ensure_subtitle_cache_schema", "publish_media", "manifest_complete",
                               "cache_record_complete", "get_valid_track", "pending_revision", "complete_pending_media",
                               "invalidate_media", "missing_cache_paths"}, scope)
load("app/subtitle_image_cache.py", {"ImageSubtitle", "image_cache_record_complete", "image_cache_work_required",
                                     "image_manifest_complete", "publish_image_media", "get_valid_image_track",
                                     "cached_image_indexes"}, scope)
load("app/subtitle_cache_worker.py", {"_file_identity", "image_track_manifest", "cache_media"}, scope)
scope["ensure_subtitle_cache_schema"]()
database.executescript("""
CREATE TABLE plex_media(path TEXT PRIMARY KEY);
CREATE TABLE media_stream_index_state(path TEXT PRIMARY KEY);
CREATE TABLE media_stream_index(path TEXT,stream_type TEXT,codec TEXT);
""")
scope["text_track_manifest"] = lambda media, metadata=None: (
    "text:" + scope["_file_identity"](media)["edge_sha256"],
    [{"source": "embedded", "type_index": 0, "external_path": "", "codec": "subrip"}]
    if metadata and metadata.get("text") else [],
)
scope["_extract_track"] = lambda _media, track, *_: scope["TextSubtitle"](
    "embedded", track["type_index"], "", "subrip", "1\n00:00:00,000 --> 00:00:01,000\nOlá\n",
)
scope["_extract_embedded_batch"] = lambda *_: {}
image_calls = []


def extract_images(_media, tracks, *_):
    image_calls.append([track["type_index"] for track in tracks])
    return [scope["ImageSubtitle"](track["type_index"], track["codec"], b"compressed-image-packets",
                                   b"VobSub index" if track["codec"] == "vobsub" else b"") for track in tracks]


scope["_extract_image_batch"] = extract_images
with TemporaryDirectory() as folder:
    media = Path(folder) / "episode.mkv"
    media.write_bytes(b"media")
    image_only = {"streams": [{"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"}]}
    mixed = {"text": True, "streams": [
        {"codec_type": "subtitle", "codec_name": "subrip"},
        {"codec_type": "subtitle", "codec_name": "dvd_subtitle"},
    ]}
    text_only = {"text": True, "streams": [{"codec_type": "subtitle", "codec_name": "subrip"}]}
    scope["probe"] = lambda _media: image_only
    scope["_indexed_text_track_presence"] = lambda _media: True

    database.execute("INSERT INTO plex_media VALUES(?)", (str(media),))
    database.execute("INSERT INTO media_stream_index_state VALUES(?)", (str(media),))
    database.execute("INSERT INTO media_stream_index VALUES(?,?,?)", (str(media), "subtitle", "HDMV PGS"))
    assert scope["cache_media"](media, image_only)["tracks"] == 1
    signature, _ = scope["image_track_manifest"](media, image_only)
    assert scope["get_valid_image_track"](str(media), signature, 0).payload == b"compressed-image-packets"
    assert scope["cached_image_indexes"](str(media), signature) == {0}
    assert scope["missing_cache_paths"]([str(media)]) == set()
    assert scope["cache_media"](media, image_only)["status"] == "cached"
    assert image_calls == [[0]]

    scope["invalidate_media"](str(media))
    database.execute("UPDATE media_stream_index SET codec='VobSub' WHERE path=?", (str(media),))
    scope["probe"] = lambda _media: mixed
    assert scope["cache_media"](media, mixed)["tracks"] == 2
    signature, _ = scope["image_track_manifest"](media, mixed)
    assert scope["get_valid_image_track"](str(media), signature, 1).index_payload == b"VobSub index"
    assert scope["cache_media"](media, mixed)["status"] == "cached"
    assert image_calls == [[0], [1]]
    database.execute("DELETE FROM subtitle_image_cache_media WHERE path=?", (str(media),))
    assert scope["missing_cache_paths"]([str(media)]) == {str(media)}
    assert scope["cache_media"](media, mixed)["status"] == "extracted"
    assert image_calls == [[0], [1], [1]]

    scope["invalidate_media"](str(media))
    database.execute("UPDATE media_stream_index SET codec='SubRip/SRT' WHERE path=?", (str(media),))
    scope["probe"] = lambda _media: text_only
    assert scope["cache_media"](media, text_only)["tracks"] == 1
    assert scope["cache_media"](media, text_only)["status"] == "cached"
    assert image_calls == [[0], [1], [1]]

    scope["invalidate_media"](str(media))
    assert scope["get_valid_image_track"](str(media), signature, 1) is None

print("PASS: image-only, mixed, and text-only media cache independently and invalidate together")
