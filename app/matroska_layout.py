"""Incremental, header-only Matroska track-layout checkpoint.

This reports a physical placement concern, not a claim that the file violates
the Matroska specification: an early SeekHead may legally point to late Tracks.
"""

from __future__ import annotations

import logging
import json
from pathlib import Path

from app.pg_compat import connect


logger = logging.getLogger("uvicorn.error")
MATROSKA_SUFFIXES = {".mkv", ".mka", ".mks", ".mk3d"}
EBML_HEADER = 0x1A45DFA3
SEGMENT = 0x18538067
TRACKS = 0x1654AE6B
CLUSTER = 0x1F43B675
MAX_TOP_LEVEL_ELEMENTS = 4096
MAX_HEADER_BYTES = 8 * 1024 * 1024


def _vint(source, *, element_id: bool) -> tuple[int, int] | None:
    first = source.read(1)
    if not first or first == b"\0":
        return None
    value = first[0]
    length = 1
    marker = 0x80
    while not value & marker:
        marker >>= 1
        length += 1
    if length > (4 if element_id else 8):
        return None
    tail = source.read(length - 1)
    if len(tail) != length - 1:
        return None
    number = int.from_bytes(first + tail, "big")
    if element_id:
        return number, length
    number &= (1 << (7 * length)) - 1
    if number == (1 << (7 * length)) - 1:
        return None, length  # Unknown size is valid for Segment/Cluster.
    return number, length


def _element(source) -> tuple[int, int | None, int] | None:
    identifier = _vint(source, element_id=True)
    size = _vint(source, element_id=False) if identifier else None
    if not identifier or not size:
        return None
    return identifier[0], size[0], source.tell()


def inspect_layout(path: Path) -> tuple[str, str]:
    """Find the first Cluster without reading media packets or late elements."""
    if path.suffix.casefold() not in MATROSKA_SUFFIXES:
        return "not_matroska", "Not a Matroska file"
    file_size = path.stat().st_size
    with path.open("rb") as source:
        header = _element(source)
        if not header or header[0] != EBML_HEADER or header[1] is None:
            return "unknown", "EBML header is unavailable or malformed"
        source.seek(header[2] + header[1])
        segment = _element(source)
        if not segment or segment[0] != SEGMENT:
            return "unknown", "Matroska Segment header was not found"
        segment_end = segment[2] + segment[1] if segment[1] is not None else file_size
        if segment_end > file_size:
            return "unknown", "Matroska Segment extends beyond the file"
        tracks_seen = False
        header_bytes = source.tell()
        for _ in range(MAX_TOP_LEVEL_ELEMENTS):
            if source.tell() >= segment_end or header_bytes > MAX_HEADER_BYTES:
                break
            before = source.tell()
            element = _element(source)
            if not element:
                return "unknown", "Cannot read a top-level Matroska element"
            identifier, size, data_start = element
            header_bytes += data_start - before
            if identifier == TRACKS:
                tracks_seen = True
            if identifier == CLUSTER:
                if tracks_seen:
                    return "ok", "Track headers precede the first media Cluster"
                return "tracks_after_cluster", "Track headers are not before the first media Cluster; an early SeekHead may still make the layout valid"
            if size is None or data_start + size > segment_end:
                return "unknown", "An earlier element has an unknown or invalid size"
            source.seek(data_start + size)
    return "unknown", "No first media Cluster found within the bounded header check"


def checkpoint(path: Path) -> str:
    """Recheck only when the media file's size or nanosecond mtime changes."""
    media = Path(path)
    if media.suffix.casefold() not in MATROSKA_SUFFIXES:
        return "not_matroska"
    stat = media.stat()
    with connect() as db:
        previous = db.execute(
            "SELECT size,modified_ns,status FROM matroska_layout_check WHERE path=?", (str(media),),
        ).fetchone()
    if previous and int(previous["size"]) == stat.st_size and int(previous["modified_ns"]) == stat.st_mtime_ns:
        return str(previous["status"])
    status, detail = inspect_layout(media)
    after = media.stat()
    if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
        return "changed_during_check"
    with connect() as db:
        db.execute(
            "INSERT INTO matroska_layout_check(path,size,modified_ns,status,detail,checked_at) "
            "VALUES(?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(path) DO UPDATE SET "
            "size=excluded.size,modified_ns=excluded.modified_ns,status=excluded.status,"
            "detail=excluded.detail,checked_at=CURRENT_TIMESTAMP",
            (str(media), stat.st_size, stat.st_mtime_ns, status, detail),
        )
    return status


def safe_checkpoint(path: Path) -> None:
    """A diagnostic must never fail its parent cache/index/media operation."""
    try:
        checkpoint(path)
    except Exception as exc:
        logger.warning("matroska_layout event=checkpoint_failed path=%s error=%s", path,
                       str(exc).replace("\n", " ")[:300])


def revalidate_warning_rows(rows) -> set[str]:
    """Refresh only flagged files that changed; never traverse the catalog."""
    current = set()
    for row in rows:
        path = str(row["path"])
        try:
            stat = Path(path).stat()
            if stat.st_size == int(row["size"]) and stat.st_mtime_ns == int(row["modified_ns"]):
                current.add(path)
            elif checkpoint(Path(path)) == "tracks_after_cluster":
                current.add(path)
        except Exception as exc:
            logger.warning("matroska_layout event=report_recheck_failed path=%s error=%s", path,
                           str(exc).replace("\n", " ")[:300])
    return current


def active_remux_paths() -> set[str]:
    """Paths already waiting in preflight or the media task queue."""
    with connect() as db:
        queued = db.execute(
            "SELECT m.path FROM media_change_request m JOIN task_queue q ON q.id=m.task_id "
            "WHERE q.task_type='matroska_layout_remux' AND q.status IN ('pending','running')"
        ).fetchall()
        preflights = db.execute(
            "SELECT payload_json FROM preflight_requests "
            "WHERE operation_type='matroska_layout_remux' AND status IN ('pending','running')"
        ).fetchall()
    paths = {str(row["path"]) for row in queued}
    for row in preflights:
        try:
            payload = json.loads(row["payload_json"] or "{}")
            paths.update(str(item["path"]) for item in payload.get("_bulk_items", []) if item.get("path"))
        except (TypeError, ValueError, KeyError):
            continue
    return paths
