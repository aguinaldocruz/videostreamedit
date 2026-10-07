"""Queued, media-scoped repair for late Matroska track headers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from app.job_safety import output_space, replace_prepared, run_write_command, stamp
from app.matroska_layout import checkpoint, inspect_layout


logger = logging.getLogger("uvicorn.error")


def _streams(path: Path) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    streams = json.loads(result.stdout).get("streams") or []
    if not streams:
        raise RuntimeError("No media streams found; remux refused")
    return [(str(item.get("codec_type") or ""), str(item.get("codec_name") or "")) for item in streams]


_TRACK_PROPERTIES = (
    "language", "language_ietf", "track_name", "default_track", "forced_track",
    "enabled_track", "flag_hearing_impaired", "flag_visual_impaired", "flag_commentary",
    "flag_original", "flag_text_descriptions",
    "codec_delay",
)
_GENERATED_TAGS = {
    "ENCODER", "BPS", "DURATION", "NUMBER_OF_FRAMES", "NUMBER_OF_BYTES",
    "_STATISTICS_WRITING_APP", "_STATISTICS_WRITING_DATE_UTC", "_STATISTICS_TAGS",
}


def _semantic_snapshot(path: Path) -> dict:
    """Compare user-visible Matroska metadata, not remux-generated IDs/dates."""
    identified = subprocess.run(
        ["mkvmerge", "-J", str(path)], capture_output=True, text=True, timeout=120, check=True,
    )
    data = json.loads(identified.stdout)
    tracks = data.get("tracks") or []
    if not tracks:
        raise RuntimeError("No Matroska tracks found; remux refused")
    uid_to_position = {
        str(track.get("properties", {}).get("uid")): position
        for position, track in enumerate(tracks)
    }
    tag_output = subprocess.run(
        ["mkvextract", str(path), "tags"], capture_output=True, timeout=120, check=True,
    )
    root = ET.fromstring(tag_output.stdout.decode("utf-8-sig"))
    meaningful_tags = []
    for tag in root.findall("Tag"):
        target = tag.find("Targets")
        track_uid = target.findtext("TrackUID") if target is not None else None
        owner = ("track", uid_to_position.get(track_uid, track_uid)) if track_uid else ("global",)
        for simple in tag.iter("Simple"):
            name = simple.findtext("Name")
            if name and name.upper() not in _GENERATED_TAGS:
                meaningful_tags.append((owner, name, simple.findtext("String"), simple.findtext("Binary")))
    chapter_counts = tuple(int(chapter.get("num_entries") or 0) for chapter in data.get("chapters") or [])
    chapters = []
    if chapter_counts:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=120, check=True,
        )
        chapters = [
            (round(float(item.get("start_time") or 0) * 1000),
             round(float(item.get("end_time") or 0) * 1000),
             tuple(sorted((item.get("tags") or {}).items())))
            for item in json.loads(probe.stdout).get("chapters") or []
        ]
    return {
        "tracks": [
            (track.get("type"), track.get("codec"),
             tuple((key, track.get("properties", {}).get(key)) for key in _TRACK_PROPERTIES))
            for track in tracks
        ],
        "attachments": sorted(
            (str(item.get("file_name") or ""), str(item.get("content_type") or ""),
             str(item.get("description") or ""), int(item.get("size") or 0))
            for item in data.get("attachments") or []
        ),
        "chapters": (chapter_counts, chapters),
        "tags": sorted(meaningful_tags, key=str),
        "title": (data.get("container") or {}).get("properties", {}).get("title"),
    }


def _needs_native_remux(snapshot: dict) -> bool:
    """FFmpeg does not preserve Matroska's IETF language elements."""
    if snapshot["attachments"] or snapshot["tags"] or snapshot["title"]:
        return True
    if snapshot["chapters"][0]:
        return True
    return any(
        (key == "language_ietf" and value) or (key == "enabled_track" and value is False)
        or (key == "codec_delay" and value is not None)
        or (key in {"flag_hearing_impaired", "flag_visual_impaired", "flag_commentary", "flag_original", "flag_text_descriptions"}
            and value is not None)
        for _kind, _codec, properties in snapshot["tracks"]
        for key, value in properties
    )


def _ffmpeg_dispositions(path: Path) -> list[str]:
    """Explicit zero matters: FFmpeg otherwise invents a first default track."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    options = []
    for position, stream in enumerate(json.loads(result.stdout).get("streams") or []):
        flags = [key for key, value in (stream.get("disposition") or {}).items() if value]
        options += [f"-disposition:{position}", "+".join(flags) or "0"]
    return options


def _subtitle_packets(path: Path) -> list[tuple]:
    """Check raw subtitle bytes and timing, without decoding invalid UTF-8."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "s", "-show_packets",
         "-show_data_hash", "sha256", "-show_entries",
         "packet=stream_index,pts_time,duration_time,data_hash", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=600, check=True,
    )
    packets = json.loads(result.stdout).get("packets") or []
    if not packets or any(not packet.get("data_hash") for packet in packets):
        raise RuntimeError("Cannot verify warning-bearing subtitle payloads; original retained")
    return sorted((int(packet["stream_index"]), packet["data_hash"],
                   round(float(packet.get("pts_time") or 0) * 1000),
                   round(float(packet.get("duration_time") or 0) * 1000)) for packet in packets)


def _verify_remux_warning(result: dict, original: Path, output: Path, *, excluded_stream_indexes: set[int] | None = None,
                          timestamp_shift_ms: int = 0) -> str | None:
    """Allow only the known source-encoding warning with byte-exact evidence."""
    if not result or result.get("returncode") != 1:
        return None
    lines = [line.strip() for line in result.get("output", "").splitlines() if line.strip()]
    known = "This text subtitle track contains invalid 8-bit characters outside valid multi-byte UTF-8 sequences."
    if result.get("truncated") or not lines or any(
        not line.startswith("Warning:") or known not in line for line in lines
    ):
        raise RuntimeError("Remux warning requires manual review; original retained: " + result.get("output", "")[-2000:])
    excluded = excluded_stream_indexes or set()
    before = [(packet[0],packet[1],packet[2]+timestamp_shift_ms,packet[3])
              for packet in _subtitle_packets(original) if packet[0] not in excluded]
    after = [packet for packet in _subtitle_packets(output) if packet[0] not in excluded]
    if not before or before != after:
        raise RuntimeError("Remux changed subtitle bytes or timing after an encoding warning; original retained")
    return "Source subtitle has non-UTF-8 bytes; raw subtitle bytes and timing were preserved, not repaired"


def _restore_language_properties(path: Path, original: dict, remuxed: dict) -> bool:
    """Restore native language and decoder-delay headers in the copy."""
    if len(original["tracks"]) != len(remuxed["tracks"]):
        return False
    edits = []
    for number, (before_track, after_track) in enumerate(zip(original["tracks"], remuxed["tracks"]), 1):
        before = dict(before_track[2])
        after = dict(after_track[2])
        track_edits = []
        if before["language"] != after["language"]:
            track_edits += ["--set", f"language={before['language'] or 'und'}"]
        # Setting the legacy code also rewrites the IETF tag, so always restore
        # the latter afterwards when the former needed correction.
        if before["language"] != after["language"] or before["language_ietf"] != after["language_ietf"]:
            if before["language_ietf"]:
                track_edits += ["--set", f"language-ietf={before['language_ietf']}"]
            else:
                track_edits += ["--delete", "language-ietf"]
        if before.get('codec_delay') != after.get('codec_delay'):
            track_edits += (['--set', f"codec-delay={before['codec_delay']}"]
                            if before.get('codec_delay') is not None else ['--delete', 'codec-delay'])
        if track_edits:
            edits += ["--edit", f"track:{number}", *track_edits]
    if not edits:
        return False
    subprocess.run(["mkvpropedit", str(path), *edits], capture_output=True, text=True,
                   timeout=120, check=True)
    return True


def ensure_front_track_headers(path: Path, *, live: bool = False) -> bool:
    """Final mutation guard; no remux for good/non-Matroska outputs.

    Run AFTER all header edits. Native packet passthrough preserves metadata
    and leaves header slack. A repair is bounded to one attempt, verified before
    publication, and never changes a rollback original. ``live`` uses the
    normal durable replacement receipt for metadata-only edits.
    """
    from app.matroska_layout import MATROSKA_SUFFIXES, safe_checkpoint
    path = Path(path)
    if path.suffix.casefold() not in MATROSKA_SUFFIXES:
        return False
    status, detail = inspect_layout(path)
    if status == "ok":
        if live:
            safe_checkpoint(path, force=True)
        return False
    if status != "tracks_after_cluster":
        raise RuntimeError(f"Cannot verify final Matroska header layout: {detail}")
    before = stamp(path)
    stat = path.stat()
    temporary = path.with_name(f".{path.stem}.vse-layout-{uuid.uuid4().hex}.mkv")
    logger.info("matroska_layout event=automatic_repair_started path=%s", path)
    try:
        with output_space(path.parent, int(stat.st_size * 1.1) + 64 * 1024**2):
            streams = _streams(path)
            semantics = _semantic_snapshot(path)
            result = run_write_command(
                ["mkvmerge", "--quiet", "--engage", "force_passthrough_packetizer",
                 "-o", str(temporary), str(path)], path.parent,
                timeout=6 * 60 * 60, accepted_returncodes=(0, 1),
            )
            after = _semantic_snapshot(temporary)
            if _restore_language_properties(temporary, semantics, after):
                after = _semantic_snapshot(temporary)
            if _streams(temporary) != streams or after != semantics:
                raise RuntimeError("Automatic layout repair changed stream metadata; input retained")
            _verify_remux_warning(result, path, temporary)
            if inspect_layout(temporary)[0] != "ok":
                raise RuntimeError("Automatic layout repair failed final header check; input retained")
            shutil.copystat(path, temporary)
            if live:
                replace_prepared(temporary, path, before)
            else:
                # Caller owns this uncommitted candidate; its normal commit
                # subsequently fsyncs it and checks the real original's stamp.
                if stamp(path) != before:
                    raise RuntimeError("Prepared media changed during layout verification")
                os.replace(temporary, path)
        if live:
            safe_checkpoint(path, force=True)
        logger.info("matroska_layout event=automatic_repair_completed path=%s", path)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def process_matroska_layout_remux(task_id: int, payload: dict) -> dict:
    from app.v11 import connection, plex_authorized_file
    from app.v65 import update_progress
    from app.v86 import assert_media_editable

    media = plex_authorized_file(str(payload["path"]))
    with connection() as db:
        if not db.execute("SELECT 1 FROM plex_media WHERE path=?", (str(media),)).fetchone():
            raise RuntimeError("Media is no longer in the Plex catalog")
    assert_media_editable(str(media))
    if media.suffix.casefold() not in {".mkv", ".mka", ".mks", ".mk3d"}:
        raise RuntimeError("Stream-copy layout repair requires a Matroska file")
    update_progress(task_id, 0, 5, "Checking current Matroska layout")
    if inspect_layout(media)[0] != "tracks_after_cluster":
        checkpoint(media, force=True)
        return {"path": str(media), "status": "skipped", "reason": "Track headers already precede the first Cluster"}

    before = stamp(media)
    original_streams = _streams(media)
    original_semantics = _semantic_snapshot(media)
    # Keep a Matroska extension: inspect_layout intentionally rejects unknown
    # extensions, even when FFmpeg was explicitly told to write Matroska.
    temporary = media.with_name(f".{media.stem}.vse-remux-{uuid.uuid4().hex}.mkv")
    warning = None
    try:
        with output_space(media.parent, before["size"]):
            # Native muxing leaves headroom for future metadata edits even for
            # otherwise simple files. FFmpeg's tight headers can immediately
            # relocate when a later editor adds a regional language tag.
            native_remux = True
            update_progress(task_id, 1, 5, "Preserving Matroska metadata in a temporary remux"
                            if native_remux else "Stream-copy remuxing to a temporary file")
            # Layout repair must not re-packetize/normalize subtitle text or
            # codec bitstreams. Generic packet passthrough retains source bytes.
            command = (["mkvmerge", "--quiet", "--engage", "force_passthrough_packetizer",
                        "-o", str(temporary), str(media)] if native_remux else
                       ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i", str(media),
                        "-map", "0", "-c", "copy", *_ffmpeg_dispositions(media),
                        "-default_mode", "passthrough", "-f", "matroska", str(temporary)])
            result = run_write_command(command, media.parent, timeout=6 * 60 * 60,
                                       accepted_returncodes=(0, 1) if native_remux else (0,))
            update_progress(task_id, 2, 5, "Verifying streams and track-header layout")
            if _streams(temporary) != original_streams:
                raise RuntimeError("Remux changed the stream count, types, or codecs; original retained")
            temporary_semantics = _semantic_snapshot(temporary)
            if native_remux and _restore_language_properties(temporary, original_semantics, temporary_semantics):
                temporary_semantics = _semantic_snapshot(temporary)
            if temporary_semantics != original_semantics:
                raise RuntimeError("Remux changed track properties, chapters, attachments, tags, or title; original retained")
            warning = _verify_remux_warning(result, media, temporary)
            if inspect_layout(temporary)[0] != "ok":
                raise RuntimeError("Remux did not move track headers before the first Cluster; original retained")
            shutil.copymode(media, temporary)
            update_progress(task_id, 3, 5, "Atomically replacing the original media")
            replace_prepared(temporary, media, before)
    finally:
        temporary.unlink(missing_ok=True)

    try:
        update_progress(task_id, 4, 5, "Rechecking layout and refreshing media indexes")
    except Exception as exc:
        logger.warning("matroska_remux event=post_commit_progress_failed path=%s error=%s", media, exc)
    try:
        result = checkpoint(media, force=True)
    except Exception as exc:
        result = "recheck_pending"
        logger.warning("matroska_remux event=post_commit_recheck_failed path=%s error=%s", media, exc)
    try:
        from app.v68 import register_internal_change_scope
        register_internal_change_scope(str(media), {}, "Matroska layout remux")
    except Exception as exc:
        logger.warning("matroska_remux event=post_commit_plex_marker_failed path=%s error=%s", media, exc)
    try:
        from app.subtitle_cache import invalidate_and_enqueue_media_many
        invalidate_and_enqueue_media_many([str(media)])
    except Exception as exc:
        logger.warning("matroska_remux event=post_commit_subtitle_cache_failed path=%s error=%s", media, exc)
    try:
        from app.v80 import request_media_indexes
        request_media_indexes(str(media), ["core"], "Matroska layout remux")
    except Exception as exc:
        logger.warning("matroska_remux event=post_commit_core_index_failed path=%s error=%s", media, exc)
    try:
        update_progress(task_id, 5, 5, "Matroska layout repair completed")
    except Exception as exc:
        logger.warning("matroska_remux event=post_commit_progress_failed path=%s error=%s", media, exc)
    if warning:
        logger.warning("matroska_remux event=source_encoding_preserved path=%s warning=%s", media, warning)
    return {"path": str(media), "status": "remuxed", "layout": result,
            "streams": len(original_streams), "warning": warning}
