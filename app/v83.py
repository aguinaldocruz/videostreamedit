from __future__ import annotations

import logging
import math
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.v2 import probe
from app.v5 import external_subtitles
from app.v28 import authorized_import_file
from app.v82 import app

logger = logging.getLogger("uvicorn.error")
review_lock = threading.Semaphore(1)
TEXT_SUBTITLE_CODECS = {"ass", "ssa", "subrip", "srt", "text", "mov_text", "webvtt", "microdvd", "jacosub", "sami", "realtext", "subviewer", "subviewer1", "vplayer"}


def filter_path(path: Path) -> str:
    value = str(path).replace("\\", "\\\\")
    for character in (":", "'", "[", "]", ",", ";"):
        value = value.replace(character, "\\" + character)
    return value


def remove_review_file(path: str) -> None:
    Path(path).unlink(missing_ok=True)


@app.get("/api/v83/media-review/clip")
def media_review_clip(
    path: str,
    audio_index: int = Query(ge=0),
    start: float = Query(default=300, ge=0),
    length: int = Query(default=60, ge=15, le=120),
    subtitle_source: str = Query(default="none", pattern="^(none|embedded|external)$"),
    subtitle_index: int | None = Query(default=None, ge=0),
    external_path: str | None = None,
) -> FileResponse:
    media = authorized_import_file(path)
    metadata = probe(media)
    streams = metadata.get("streams") or []
    audios = [item for item in streams if item.get("codec_type") == "audio"]
    subtitles = [item for item in streams if item.get("codec_type") == "subtitle"]
    if audio_index >= len(audios):
        raise HTTPException(400, "The selected audio stream is no longer available")
    duration = float((metadata.get("format") or {}).get("duration") or 0)
    if duration:
        start = min(start, max(0, duration - 1))
        length = max(1, min(length, math.ceil(duration - start)))

    video_filter = "scale=w='min(1280,iw)':h=-2"
    complex_filter: str | None = None
    selected_external: Path | None = None
    if subtitle_source == "embedded":
        if subtitle_index is None or subtitle_index >= len(subtitles):
            raise HTTPException(400, "The selected subtitle stream is no longer available")
        codec = str(subtitles[subtitle_index].get("codec_name") or "").lower()
        if codec in TEXT_SUBTITLE_CODECS:
            video_filter = f"setpts=PTS+{start}/TB,subtitles='{filter_path(media)}':si={subtitle_index},setpts=PTS-STARTPTS,{video_filter}"
        else:
            complex_filter = f"[0:v:0][0:s:{subtitle_index}]overlay,{video_filter}[review_video]"
    elif subtitle_source == "external":
        candidates = {str(item["path"]): Path(item["path"]) for item in external_subtitles(media)}
        selected_external = candidates.get(str(external_path or ""))
        if not selected_external:
            raise HTTPException(400, "The selected external subtitle does not belong to this media")
        if selected_external.suffix.lower() in {".sub", ".idx", ".sup"}:
            raise HTTPException(422, "This external bitmap subtitle format cannot be rendered in the review viewer")
        video_filter = f"setpts=PTS+{start}/TB,subtitles='{filter_path(selected_external)}',setpts=PTS-STARTPTS,{video_filter}"

    temporary = tempfile.NamedTemporaryFile(prefix="vse-review-", suffix=".mp4", delete=False)
    output = temporary.name
    temporary.close()
    command = ["nice", "-n", "5", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-i", str(media), "-t", str(length)]
    if complex_filter:
        command += ["-filter_complex", complex_filter, "-map", "[review_video]"]
    else:
        command += ["-vf", video_filter, "-map", "0:v:0"]
    command += ["-map", f"0:a:{audio_index}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", "-y", output]
    logger.info("media_review event=clip_started file=%s audio=%d subtitle=%s start=%.0f", str(media).replace("\n", "\\n"), audio_index, subtitle_source, start)
    try:
        with review_lock:
            result = subprocess.run(command, capture_output=True, timeout=300)
        if result.returncode or not Path(output).is_file() or Path(output).stat().st_size == 0:
            detail = result.stderr.decode("utf-8", errors="replace")[-1800:]
            raise HTTPException(422, detail or "Could not create the media review clip")
    except subprocess.TimeoutExpired as exc:
        Path(output).unlink(missing_ok=True)
        raise HTTPException(504, "Media review generation exceeded five minutes") from exc
    except Exception:
        if not Path(output).is_file() or Path(output).stat().st_size == 0:
            Path(output).unlink(missing_ok=True)
        raise
    logger.info("media_review event=clip_ready file=%s bytes=%d start=%.0f", str(media).replace("\n", "\\n"), Path(output).stat().st_size, start)
    headers = {"X-Media-Duration": str(duration), "X-Review-Start": str(start), "Cache-Control": "no-store"}
    return FileResponse(output, media_type="video/mp4", filename="review.mp4", headers=headers, background=BackgroundTask(remove_review_file, output))
