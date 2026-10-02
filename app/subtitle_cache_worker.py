"""Bounded, source-verified extraction primitives for complete text subtitles.

The scheduled worker is intentionally separate: these functions process one
media at a time and never start a catalog-wide scan on import or startup.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from app.pg_compat import connect
from app.subtitle_cache import CACHE_FORMAT_VERSION, TextSubtitle, cache_record_complete, complete_pending_media, enqueue_media, get_valid_media, get_valid_track, invalidate_media, manifest_complete, pending_revision, publish_media, publish_track
from app.v2 import probe
from app.v5 import external_subtitles


TEXT_CODECS = {"ass", "ssa", "subrip", "srt", "text", "mov_text", "webvtt", "microdvd", "jacosub", "sami", "realtext", "subviewer", "subviewer1", "vplayer"}
TEXT_SIDECAR_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}
MAX_TEXT_BYTES = 32 * 1024 * 1024


def _file_identity(path: Path) -> dict:
    stat = path.stat()
    with path.open("rb") as source:
        first = source.read(65536)
        if stat.st_size > 65536:
            source.seek(max(0, stat.st_size - 65536))
        last = source.read(65536)
    return {
        "path": str(path), "size": stat.st_size, "modified_ns": stat.st_mtime_ns,
        "edge_sha256": hashlib.sha256(first + last).hexdigest(),
    }


def text_track_manifest(media: Path, metadata: dict | None = None) -> tuple[str, list[dict]]:
    """Return a subtitle-only source identity and eligible text track list.

    Language, region and track name are excluded: changing those values does
    not change the subtitle text. Source-file identity is conservative, so a
    metadata-only rewrite can cause an unnecessary recache, never stale text.
    """
    metadata = metadata if metadata is not None else probe(media)
    tracks = []
    subtitle_index = 0
    for stream in metadata.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").casefold()
        if codec in TEXT_CODECS:
            tracks.append({"source": "embedded", "type_index": subtitle_index, "external_path": "", "codec": codec})
        subtitle_index += 1
    for item in external_subtitles(media):
        sidecar = Path(item["path"])
        if sidecar.suffix.casefold() in TEXT_SIDECAR_SUFFIXES and sidecar.is_file():
            tracks.append({"source": "external", "type_index": -1, "external_path": str(sidecar), "codec": sidecar.suffix[1:].casefold()})
    identity = {
        "version": 1,
        "media": _file_identity(media),
        "tracks": [(t["source"], t["type_index"], t["external_path"], t["codec"]) for t in tracks],
        "sidecars": [_file_identity(Path(t["external_path"])) for t in tracks if t["source"] == "external"],
    }
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return signature, tracks


def _extract_track(media: Path, track: dict, timeout_seconds: float = 300, low_priority: bool = False) -> TextSubtitle:
    if track["source"] == "embedded":
        source = media
        selector = f"0:s:{track['type_index']}"
    else:
        source = Path(track["external_path"])
        selector = "0:0"
    try:
        result = subprocess.run(
            (["nice", "-n", "10"] if low_priority else []) +
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-i", str(source),
             "-map", selector, "-f", "srt", "pipe:1"],
            capture_output=True, timeout=max(1, min(300, timeout_seconds)), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Complete subtitle extraction timed out for {selector}") from exc
    if result.returncode or not result.stdout or len(result.stdout) > MAX_TEXT_BYTES:
        detail = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"Complete subtitle extraction failed for {selector}: {detail or 'empty or oversized output'}")
    text = result.stdout.decode("utf-8", errors="strict")
    return TextSubtitle(track["source"], track["type_index"], track["external_path"], track["codec"], text)


def _extract_embedded_batch(media: Path, tracks: list[dict], timeout_seconds: float = 300,
                            low_priority: bool = False) -> dict[int, TextSubtitle]:
    """Demux embedded text tracks once into separate temporary SRT outputs.

    A failed FFmpeg invocation contributes no data. Missing or invalid outputs
    are left to the caller's isolated per-track retry. The caller publishes the
    whole media only after every expected track is valid and the source matches.
    """
    if len(tracks) < 2:
        return {}
    with tempfile.TemporaryDirectory(prefix="vse-subtitle-batch-") as folder:
        outputs = [(track, Path(folder) / f"subtitle-{track['type_index']}.srt") for track in tracks]
        command = (["nice", "-n", "10"] if low_priority else []) + [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1", "-i", str(media),
        ]
        for track, output in outputs:
            command.extend(["-map", f"0:s:{track['type_index']}", "-f", "srt", str(output)])
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=max(1, min(300, timeout_seconds)), check=False)
        except subprocess.TimeoutExpired:
            return {}
        if result.returncode:
            return {}
        extracted = {}
        for track, output in outputs:
            if not output.is_file() or not 0 < output.stat().st_size <= MAX_TEXT_BYTES:
                continue
            try:
                text = output.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            extracted[track["type_index"]] = TextSubtitle(
                "embedded", track["type_index"], "", track["codec"], text,
            )
        return extracted


def _indexed_text_track_presence(media: Path) -> bool | None:
    """Use the core index to avoid probing media known to have no subtitles.

    A newly added sidecar is checked on disk because it can arrive before the
    core-index job runs and does not change the media file's fingerprint.
    If indexing has not run, fall through to the normal probe. The core index
    and external-sidecar watcher own invalidation when the source changes.
    """
    with connect() as db:
        state = db.execute("SELECT 1 FROM media_stream_index_state WHERE path=?", (str(media),)).fetchone()
        if not state:
            return None
        found = db.execute(
            "SELECT 1 FROM media_stream_index WHERE path=? AND stream_type IN ('subtitle','external') LIMIT 1",
            (str(media),),
        ).fetchone()
    if found:
        return True
    return any(Path(item["path"]).suffix.casefold() in TEXT_SIDECAR_SUFFIXES for item in external_subtitles(media))


def cache_media(media: Path, metadata: dict | None = None, remaining_seconds: Callable[[], float] | None = None, low_priority: bool = False) -> dict:
    """Extract all text tracks as one media unit, skipping a valid full cache."""
    from app.matroska_layout import safe_checkpoint
    safe_checkpoint(media)
    requested_revision = pending_revision(str(media))
    if metadata is None and requested_revision is None and cache_record_complete(str(media)):
        return {"path": str(media), "status": "cached", "tracks": None}
    if metadata is None and _indexed_text_track_presence(media) is False:
        invalidate_media(str(media))
        complete_pending_media(str(media), requested_revision)
        return {"path": str(media), "status": "no_subtitles", "tracks": 0}
    signature, tracks = text_track_manifest(media, metadata)
    if manifest_complete(str(media), signature, len(tracks)):
        complete_pending_media(str(media), requested_revision)
        return {"path": str(media), "status": "cached", "tracks": len(tracks)}
    expected = {(t["source"], t["type_index"], t["external_path"]) for t in tracks}
    extracted = []
    cached_tracks = {}
    missing_embedded = []
    for track in tracks:
        key = (track["source"], track["type_index"], track["external_path"])
        cached = get_valid_track(str(media), signature, key)
        if cached:
            cached_tracks[key] = cached
        elif track["source"] == "embedded":
            missing_embedded.append(track)
    batched = {}
    if len(missing_embedded) > 1:
        remaining = remaining_seconds() if remaining_seconds else 300
        if remaining <= 1:
            raise InterruptedError("Subtitle cache run time limit reached")
        batched = _extract_embedded_batch(media, missing_embedded, remaining, low_priority)
    for track in tracks:
        remaining = remaining_seconds() if remaining_seconds else 300
        if remaining <= 1:
            raise InterruptedError("Subtitle cache run time limit reached")
        key = (track["source"], track["type_index"], track["external_path"])
        ready = cached_tracks.get(key) or (batched.get(track["type_index"]) if track["source"] == "embedded" else None)
        if ready:
            extracted.append(ready)
            continue
        try:
            extracted.append(_extract_track(media, track, remaining, low_priority))
        except RuntimeError as exc:
            if remaining_seconds and remaining_seconds() <= 1:
                raise InterruptedError("Subtitle cache run time limit reached") from exc
            raise
    if remaining_seconds and remaining_seconds() <= 0:
        raise InterruptedError("Subtitle cache run time limit reached")
    if text_track_manifest(media)[0] != signature:
        raise RuntimeError("Media or external subtitle changed during extraction; cache not published")
    publish_media(str(media), signature, expected, extracted)
    complete_pending_media(str(media), requested_revision)
    return {"path": str(media), "status": "extracted", "tracks": len(tracks)}


def cached_or_extract_track(media: Path, source: str, type_index: int, external_path: str = "", metadata: dict | None = None) -> TextSubtitle:
    """Media Review read-through: extract only the selected track on a miss."""
    from app.matroska_layout import safe_checkpoint
    safe_checkpoint(media)
    signature, tracks = text_track_manifest(media, metadata)
    key = source, type_index, external_path
    track = next((item for item in tracks if (item["source"], item["type_index"], item["external_path"]) == key), None)
    if track is None:
        raise ValueError("Selected text subtitle is not in the current media manifest")
    cached = get_valid_track(str(media), signature, key)
    if cached:
        return cached
    extracted = _extract_track(media, track)
    if text_track_manifest(media)[0] != signature:
        raise RuntimeError("Subtitle changed during extraction; retry after reindexing")
    expected = {(t["source"], t["type_index"], t["external_path"]) for t in tracks}
    publish_track(str(media), signature, expected, extracted)
    return extracted


def cache_status(media: Path, metadata: dict | None = None) -> dict:
    """Derive catalog cache state from the current source, never a stale flag."""
    signature, tracks = text_track_manifest(media, metadata)
    with connect() as db:
        row = db.execute(
            "SELECT source_signature,format_version,expected_tracks,cached_tracks "
            "FROM subtitle_cache_media WHERE path=?", (str(media),),
        ).fetchone()
    if not row:
        state, cached = "not_cached", 0
    elif row["source_signature"] != signature or row["format_version"] != CACHE_FORMAT_VERSION or row["expected_tracks"] != len(tracks):
        state, cached = "needs_refresh", 0
    elif get_valid_media(str(media), signature) is not None:
        state, cached = "cached", len(tracks)
    else:
        cached = int(row["cached_tracks"])
        state = "partial" if cached else "not_cached"
    return {"path": str(media), "status": state, "cached_tracks": cached, "expected_tracks": len(tracks)}


def cached_track_text(media: Path, source: str, type_index: int, external_path: str = "", metadata: dict | None = None) -> str | None:
    """Return verified cached text without extracting or changing cache state."""
    signature, tracks = text_track_manifest(media, metadata)
    key = (source, type_index, external_path)
    if key not in {(t["source"], t["type_index"], t["external_path"]) for t in tracks}:
        return None
    track = get_valid_track(str(media), signature, key)
    return track.text if track else None


def publish_edited_track(media: Path, source: str, type_index: int, external_path: str, verified_srt: str) -> None:
    """Retire the old media cache and publish only the verified changed track."""
    invalidate_media(str(media))
    signature, tracks = text_track_manifest(media)
    key = (source, type_index, external_path)
    selected = next((t for t in tracks if (t["source"], t["type_index"], t["external_path"]) == key), None)
    if not selected:
        return
    expected = {(t["source"], t["type_index"], t["external_path"]) for t in tracks}
    publish_track(str(media), signature, expected,
                  TextSubtitle(source, type_index, external_path, selected["codec"], verified_srt))
    if text_track_manifest(media)[0] != signature:
        invalidate_media(str(media))
        raise RuntimeError("Media changed while publishing edited subtitle cache")
    if len(expected) > 1:
        enqueue_media(str(media))


def ordered_catalog_candidates(limit: int = 200, offset: int = 0) -> list[str]:
    """Final revision first, newest Plex addition first, with stable ties."""
    if not 1 <= limit <= 1000 or offset < 0:
        raise ValueError("Invalid candidate page")
    with connect() as db:
        rows = db.execute("""
            SELECT p.path
              FROM plex_media p
              LEFT JOIN media_notes n ON n.entity_type=CASE WHEN p.kind='movie' THEN 'movie' ELSE 'tv' END
               AND n.entity_key=CASE WHEN p.kind='movie' THEN p.path ELSE 'episode:'||p.path END
              LEFT JOIN media_notes sn ON p.kind='episode' AND sn.entity_type='tv'
               AND sn.entity_key=p.library_key||':'||COALESCE(p.show_title,'Unknown show')
             ORDER BY CASE WHEN COALESCE(n.final_version,0)=1 OR COALESCE(sn.final_version,0)=1 THEN 1 ELSE 0 END DESC,
                      p.plex_added_at DESC, p.path
             LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()
    return [row["path"] for row in rows]
