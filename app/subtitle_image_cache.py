"""Source-verified cache of extracted graphical subtitle streams.

This stores the compressed source stream, not rendered frames or OCR output.
PGS has one SUP payload; VobSub has SUB and IDX payloads. Text-subtitle
consumers deliberately remain separate until graphical editing is implemented.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.pg_compat import connect


IMAGE_CACHE_FORMAT_VERSION = 1
IMAGE_CODECS = {"hdmv_pgs_subtitle": "pgs", "dvd_subtitle": "vobsub"}
MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_IMAGE_INDEX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class ImageSubtitle:
    type_index: int
    codec: str
    payload: bytes
    index_payload: bytes = b""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload + self.index_payload).hexdigest()


def image_cache_record_complete(path: str) -> bool:
    """Cheap scheduler check; core/sidecar change hooks invalidate the row."""
    with connect() as db:
        row = db.execute(
            "SELECT expected_tracks FROM subtitle_image_cache_media WHERE path=? AND format_version=?",
            (path, IMAGE_CACHE_FORMAT_VERSION),
        ).fetchone()
        if not row:
            return False
        count = db.execute("SELECT count(*) AS n FROM subtitle_image_cache_track WHERE path=?", (path,)).fetchone()["n"]
    return int(row["expected_tracks"]) == int(count)


def image_cache_work_required(path: str) -> bool:
    """Avoid probing already indexed media with no supported image subtitles."""
    with connect() as db:
        indexed = db.execute("SELECT 1 FROM media_stream_index_state WHERE path=?", (path,)).fetchone()
        if not indexed:
            return not image_cache_record_complete(path)
        image = db.execute(
            "SELECT 1 FROM media_stream_index WHERE path=? AND stream_type='subtitle' "
            "AND codec IN ('HDMV PGS','VobSub') LIMIT 1", (path,),
        ).fetchone()
    return bool(image) and not image_cache_record_complete(path)


def image_manifest_complete(path: str, source_signature: str, expected_tracks: int) -> bool:
    with connect() as db:
        row = db.execute(
            "SELECT source_signature,format_version,expected_tracks FROM subtitle_image_cache_media WHERE path=?",
            (path,),
        ).fetchone()
        if not row or row["source_signature"] != source_signature or row["format_version"] != IMAGE_CACHE_FORMAT_VERSION:
            return False
        if int(row["expected_tracks"]) != expected_tracks:
            return False
        count = db.execute("SELECT count(*) AS n FROM subtitle_image_cache_track WHERE path=?", (path,)).fetchone()["n"]
    return count == expected_tracks


def publish_image_media(path: str, source_signature: str, expected_indexes: set[int], tracks: list[ImageSubtitle]) -> None:
    """Publish all image tracks for one media atomically, never a partial set."""
    if not path or not source_signature or {track.type_index for track in tracks} != expected_indexes:
        raise ValueError("Image cache does not match the complete media manifest")
    if len(tracks) != len(expected_indexes):
        raise ValueError("Duplicate image subtitle track")
    for track in tracks:
        if track.codec not in IMAGE_CODECS.values() or not 0 < len(track.payload) <= MAX_IMAGE_BYTES:
            raise ValueError("Invalid image subtitle payload")
        if track.codec == "vobsub" and not 0 < len(track.index_payload) <= MAX_IMAGE_INDEX_BYTES:
            raise ValueError("VobSub requires its IDX payload")
        if track.codec == "pgs" and track.index_payload:
            raise ValueError("PGS must not contain an IDX payload")
    with connect() as db:
        db.execute("DELETE FROM subtitle_image_cache_media WHERE path=?", (path,))
        db.execute(
            "INSERT INTO subtitle_image_cache_media(path,source_signature,format_version,expected_tracks) "
            "VALUES(?,?,?,?)", (path, source_signature, IMAGE_CACHE_FORMAT_VERSION, len(tracks)),
        )
        for track in tracks:
            db.execute(
                "INSERT INTO subtitle_image_cache_track(path,type_index,codec,payload,index_payload,sha256) "
                "VALUES(?,?,?,?,?,?)",
                (path, track.type_index, track.codec, track.payload, track.index_payload, track.sha256),
            )


def get_valid_image_track(path: str, source_signature: str, type_index: int) -> ImageSubtitle | None:
    """Future graphical consumers may read this without re-extracting media."""
    with connect() as db:
        manifest = db.execute(
            "SELECT source_signature,format_version FROM subtitle_image_cache_media WHERE path=?", (path,),
        ).fetchone()
        if not manifest or manifest["source_signature"] != source_signature or manifest["format_version"] != IMAGE_CACHE_FORMAT_VERSION:
            return None
        row = db.execute(
            "SELECT codec,payload,index_payload,sha256 FROM subtitle_image_cache_track "
            "WHERE path=? AND type_index=?", (path, type_index),
        ).fetchone()
    if not row:
        return None
    track = ImageSubtitle(type_index, row["codec"], bytes(row["payload"]), bytes(row["index_payload"]))
    return track if track.sha256 == row["sha256"] else None


def cached_image_indexes(path: str, source_signature: str) -> set[int]:
    """Lightweight UI status without transferring binary payloads from PostgreSQL."""
    with connect() as db:
        manifest = db.execute(
            "SELECT source_signature,format_version FROM subtitle_image_cache_media WHERE path=?", (path,),
        ).fetchone()
        if not manifest or manifest["source_signature"] != source_signature or manifest["format_version"] != IMAGE_CACHE_FORMAT_VERSION:
            return set()
        rows = db.execute("SELECT type_index FROM subtitle_image_cache_track WHERE path=?", (path,)).fetchall()
    return {int(row["type_index"]) for row in rows}
