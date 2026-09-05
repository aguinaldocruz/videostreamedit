from __future__ import annotations

import html
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

import app.v38 as movie_index
from app.v2 import probe
from app.v5 import checked_external, external_subtitles
from app.v11 import connection
from app.v28 import authorized_import_file
from app.v50 import app


logger = logging.getLogger("uvicorn.error")
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
ASS_TAG = re.compile(r"\{\\[^}]+}")


class SubtitleCleanup(BaseModel):
    model_config = {"extra": "forbid"}
    path: str
    type_index: int | None = None
    external_path: str | None = None


@app.on_event("startup")
def initialize_extended_subtitle_index() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS subtitle_extended_index (
                path TEXT NOT NULL, source TEXT NOT NULL, type_index INTEGER NOT NULL DEFAULT -1,
                external_path TEXT NOT NULL DEFAULT '', codec TEXT NOT NULL DEFAULT '',
                encoding TEXT NOT NULL DEFAULT '', markup TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(path,source,type_index,external_path)
            );
            CREATE INDEX IF NOT EXISTS subtitle_extended_filter ON subtitle_extended_index(encoding,markup,path);
        """)


def decode_external(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace"), "UTF-8 BOM"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "UTF-16"
    try:
        return data.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "Windows-1252"


def markup_kind(text: str) -> str:
    kinds = []
    if HTML_TAG.search(text):
        kinds.append("HTML tags")
    if ASS_TAG.search(text):
        kinds.append("ASS styling")
    return " + ".join(kinds) or "None"


def extracted_text(path: Path, selector: str) -> str:
    try:
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", selector, "-t", "900", "-f", "srt", "pipe:1"], capture_output=True, timeout=50, check=True)
        return result.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def complete_extracted_text(path: Path, selector: str) -> str:
    """Extract the complete subtitle stream for a destructive cleanup operation."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", selector, "-f", "srt", "pipe:1"],
            capture_output=True,
            timeout=180,
            check=True,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def inspect_extended(path: Path) -> list[tuple]:
    found = []
    subtitle_index = 0
    for stream in probe(path).get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "unknown")
        if codec in TEXT_SUBTITLE_CODECS:
            text = extracted_text(path, f"0:s:{subtitle_index}")
            encoding, markup = "UTF-8 (container)", markup_kind(text)
        else:
            encoding, markup = "Bitmap", "Graphical"
        found.append((str(path), "embedded", subtitle_index, "", codec, encoding, markup))
        subtitle_index += 1
    for item in external_subtitles(path):
        subtitle = Path(item["path"])
        raw = subtitle.read_bytes()[:512_000]
        text, encoding = decode_external(raw)
        found.append((str(path), "external", -1, str(subtitle), item.get("codec") or subtitle.suffix.lstrip("."), encoding, markup_kind(text)))
    return found


_original_run_index = movie_index._run_index


def extended_run_index(items: list[dict]) -> None:
    # Preserve the established index lifecycle, then add the heavier subtitle scan.
    _original_run_index(items)
    with movie_index._index_lock:
        movie_index._index_state.update(running=bool(items), total=len(items), completed=0)
    for completed, item in enumerate(items, 1):
        path = Path(item["path"])
        if not path.is_file():
            continue
        try:
            values = inspect_extended(path)
            with connection() as db:
                db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (str(path),))
                db.executemany("INSERT INTO subtitle_extended_index(path,source,type_index,external_path,codec,encoding,markup) VALUES(?,?,?,?,?,?,?)", values)
        except Exception as exc:
            logger.warning("change=subtitle_extended_index_failed path=%s error=%s", str(path).replace("\n", "\\n"), exc)
        with movie_index._index_lock:
            movie_index._index_state["completed"] = completed
    with movie_index._index_lock:
        movie_index._index_state["running"] = False
    logger.info("change=subtitle_extended_index_completed media=%d", len(items))


movie_index._run_index = extended_run_index


def filter_values() -> dict:
    base = movie_index.movie_stream_filter_values()
    with connection() as db:
        base["subtitle_encodings"] = [row[0] for row in db.execute("SELECT DISTINCT encoding FROM subtitle_extended_index WHERE encoding!='' ORDER BY encoding COLLATE NOCASE")]
        base["subtitle_markup"] = [row[0] for row in db.execute("SELECT DISTINCT markup FROM subtitle_extended_index WHERE markup!='' ORDER BY markup COLLATE NOCASE")]
    return base


@app.get("/api/v51/movies/stream-filter-values")
def extended_filter_values() -> dict:
    return filter_values()


@app.get("/api/v51/movies/stream-filter-matches")
def extended_filter_matches(stream_type: str = Query(default="all", pattern="^(all|audio|subtitle)$"), language: str = "", track_name: str = "", subtitle_encoding: str = "", subtitle_markup: str = "") -> dict:
    base_paths = set(movie_index.movie_stream_filter_matches(stream_type, language, track_name)["paths"])
    if not subtitle_encoding and not subtitle_markup:
        return {"paths": sorted(base_paths)}
    clauses, values = ["1=1"], []
    if subtitle_encoding:
        clauses.append("encoding=?"); values.append(subtitle_encoding)
    if subtitle_markup:
        clauses.append("markup=?"); values.append(subtitle_markup)
    with connection() as db:
        extended = {row[0] for row in db.execute("SELECT DISTINCT path FROM subtitle_extended_index WHERE " + " AND ".join(clauses), values)}
    return {"paths": sorted(base_paths & extended)}


@app.post("/api/v51/setup/movie-index/rebuild")
def rebuild_extended_index() -> dict:
    with connection() as db:
        db.execute("DELETE FROM subtitle_extended_index")
    return movie_index.rebuild_movie_index()


@app.get("/api/v51/subtitle-properties")
def subtitle_properties(path: str) -> dict:
    with connection() as db:
        rows = db.execute("SELECT source,type_index,external_path,codec,encoding,markup FROM subtitle_extended_index WHERE path=? ORDER BY source,type_index,external_path", (str(Path(path)),)).fetchall()
    return {"properties": [dict(row) for row in rows]}


def strip_html(text: str) -> str:
    return html.unescape(HTML_TAG.sub("", text))


def clean_external_html(subtitle: Path) -> None:
    text, current = decode_external(subtitle.read_bytes())
    if not HTML_TAG.search(text):
        logger.info("change=subtitle_html_cleanup_skipped reason=no_tags file=%s source=external", str(subtitle).replace("\n", "\\n"))
        return
    codecs = {"UTF-8": "utf-8", "UTF-8 BOM": "utf-8-sig", "UTF-16": "utf-16", "Windows-1252": "cp1252"}
    temporary = subtitle.with_name(f".{subtitle.name}.vse.tmp")
    try:
        temporary.write_bytes(strip_html(text).encode(codecs[current], errors="replace"))
        os.chmod(temporary, subtitle.stat().st_mode)
        os.replace(temporary, subtitle)
    finally:
        temporary.unlink(missing_ok=True)


def clean_embedded(media: Path, type_index: int) -> None:
    streams = probe(media).get("streams", [])
    subtitle_globals = [index for index, stream in enumerate(streams) if stream.get("codec_type") == "subtitle"]
    if type_index < 0 or type_index >= len(subtitle_globals):
        raise HTTPException(404, "Subtitle stream was not found")
    selected_global = subtitle_globals[type_index]
    # Cleanup must inspect the whole stream. Some valid subtitles have their
    # first event well after the 15-minute indexing sample.
    text = complete_extracted_text(media, f"0:s:{type_index}")
    if not text:
        raise HTTPException(422, "Only text subtitles can have markup removed")
    if not HTML_TAG.search(text):
        # Cleanup requests may have been queued from an older preview/index or
        # the same stream may already have been cleaned. Treat that as complete
        # instead of aborting unrelated edits in the same task.
        logger.info("change=subtitle_html_cleanup_skipped reason=no_tags file=%s stream=subtitle:%d", str(media).replace("\n", "\\n"), type_index)
        return
    clean = strip_html(text)
    with tempfile.TemporaryDirectory(prefix="vse-subtitle-") as folder:
        subtitle = Path(folder) / "clean.srt"
        subtitle.write_text(clean, encoding="utf-8")
        temporary = media.with_name(f".{media.stem}.subtitle-clean.vse{media.suffix}")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media), "-i", str(subtitle)]
        subtitle_output = 0
        for global_index, stream in enumerate(streams):
            if global_index == selected_global:
                command += ["-map", "1:0"]
            else:
                command += ["-map", f"0:{global_index}"]
            if stream.get("codec_type") == "subtitle":
                if global_index == selected_global:
                    command += [f"-c:s:{subtitle_output}", "srt"]
                subtitle_output += 1
        command += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
        selected = streams[selected_global]
        tags = selected.get("tags") or {}
        if tags.get("language"):
            command += [f"-metadata:s:s:{type_index}", f"language={tags["language"]}"]
        if tags.get("title") is not None:
            command += [f"-metadata:s:s:{type_index}", f"title={tags.get("title") or ""}"]
        disposition = selected.get("disposition") or {}
        flags = "+".join(name for name, enabled in disposition.items() if enabled) or "0"
        command += [f"-disposition:s:{type_index}", flags, str(temporary)]
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
            os.chmod(temporary, media.stat().st_mode)
            os.replace(temporary, media)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(422, (getattr(exc, "stderr", None) or "Subtitle cleanup failed")[-1600:]) from exc


@app.post("/api/v51/subtitle-cleanup")
def apply_subtitle_cleanup(request: SubtitleCleanup) -> dict:
    media = authorized_import_file(request.path)
    if request.external_path:
        subtitle = checked_external(media, request.external_path)
        clean_external_html(subtitle)
        target = subtitle.name
    else:
        clean_embedded(media, request.type_index if request.type_index is not None else -1)
        target = f"subtitle:{request.type_index}"
    with connection() as db:
        db.execute("DELETE FROM movie_stream_index WHERE path=?", (str(media),))
        db.execute("DELETE FROM movie_stream_index_value WHERE path=?", (str(media),))
        db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (str(media),))
    logger.info("change=subtitle_html_removed file=%s target=%s", str(media).replace("\n", "\\n"), target.replace("\n", "\\n"))
    return {"changed": True, "path": str(media)}
