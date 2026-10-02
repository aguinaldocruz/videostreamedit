"""Queued, media-scoped repair for late Matroska track headers."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import uuid
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
        checkpoint(media)
        return {"path": str(media), "status": "skipped", "reason": "Track headers already precede the first Cluster"}

    before = stamp(media)
    original_streams = _streams(media)
    # Keep a Matroska extension: inspect_layout intentionally rejects unknown
    # extensions, even when FFmpeg was explicitly told to write Matroska.
    temporary = media.with_name(f".{media.stem}.vse-remux-{uuid.uuid4().hex}.mkv")
    try:
        with output_space(media.parent, before["size"]):
            update_progress(task_id, 1, 5, "Stream-copy remuxing to a temporary file")
            run_write_command(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n", "-i", str(media),
                 "-map", "0", "-c", "copy", "-f", "matroska", str(temporary)],
                media.parent, timeout=6 * 60 * 60,
            )
            update_progress(task_id, 2, 5, "Verifying streams and track-header layout")
            if _streams(temporary) != original_streams:
                raise RuntimeError("Remux changed the stream count, types, or codecs; original retained")
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
        result = checkpoint(media)
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
    return {"path": str(media), "status": "remuxed", "layout": result, "streams": len(original_streams)}
