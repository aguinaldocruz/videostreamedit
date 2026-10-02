from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

import app.v38 as movie_index
from app.v2 import probe
from app.v5 import checked_external, external_subtitles
from app.v11 import column_exists, connection
from app.v28 import authorized_import_file
from app.v50 import app
from app.subtitle_html import has_removable_html, strip_non_color_html

logger = logging.getLogger("uvicorn.error")
TEXT_SUBTITLE_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}
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
                encoding TEXT NOT NULL DEFAULT '', markup TEXT NOT NULL DEFAULT '', damage TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(path,source,type_index,external_path)
            );
            CREATE INDEX IF NOT EXISTS subtitle_extended_filter ON subtitle_extended_index(encoding,markup,path);
        """)
        if not column_exists(db, "subtitle_extended_index", "damage"):
            db.execute("ALTER TABLE subtitle_extended_index ADD COLUMN damage TEXT NOT NULL DEFAULT ''")


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
    if has_removable_html(text):
        kinds.append("HTML tags")
    if ASS_TAG.search(text):
        kinds.append("ASS styling")
    return " + ".join(kinds) or "None"


def extracted_text(path: Path, selector: str) -> str:
    try:
        result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", selector, "-t", "900", "-f", "srt", "pipe:1"], capture_output=True, timeout=50, check=True)
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as exc:
        # FFmpeg may return a non-zero code after emitting usable subtitle
        # packets (for example, one malformed legacy character). Preserve the
        # text so markup detection can inspect the current stream.
        return (exc.stdout or b"").decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
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
    except subprocess.CalledProcessError as exc:
        raise HTTPException(422, 'Complete subtitle extraction failed; refusing to replace it with partial text') from exc
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, 'Complete subtitle extraction unavailable or timed out; original retained') from exc


def cached_subtitle_text(media: Path, source: str, type_index: int, external_path: str = "", snapshot: tuple | None = None) -> str | None:
    """Use only source-verified complete text; a miss keeps the old read path."""
    try:
        if snapshot is not None:
            from app.subtitle_cache import get_valid_track
            signature, keys = snapshot
            key = (source, type_index, external_path)
            if key not in keys:
                return None
            track = get_valid_track(str(media), signature, key)
            return track.text if track else None
        from app.subtitle_cache_worker import cached_track_text
        return cached_track_text(media, source, type_index, external_path)
    except Exception as exc:
        logger.warning("subtitle_cache event=read_unavailable path=%s error=%s", media, str(exc).replace("\n", " ")[:240])
        return None


def damage_kind(text: str) -> str:
    """Return conservative SRT corruption markers, not ordinary accented text."""
    issues = []
    if "\ufffd" in text:
        issues.append("Replacement characters")
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    if controls:
        issues.append("Control characters")
    cue_numbers = len(re.findall(r"(?m)^\s*\d+\s*$", text))
    timings = len(re.findall(r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}", text))
    if cue_numbers and not timings:
        issues.append("Malformed SRT timing")
    if re.search(r"(?:Ã.|Â.|â€|â€™|â€œ|â€)", text):
        issues.append("Possible mojibake")
    # Do not classify a subtitle merely because its language is outside the
    # configured detector. Only flag substantial payloads with almost no
    # Unicode letters at all, which is characteristic of broken OCR/decoding.
    payload = re.sub(r"^\s*\d+\s*$|^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->.*$", " ", text, flags=re.MULTILINE)
    printable = "".join(char for char in payload if char.isprintable() and not char.isspace())
    letters = sum(char.isalpha() for char in printable)
    if len(printable) >= 40 and letters < max(4, len(printable) // 12) and not re.search(r"[♪♫]", payload):
        issues.append("No recognizable text")
    # A common failed bitmap-OCR signature is many isolated letters instead of
    # words. Require a meaningful sample so short dialogue such as "I am" is
    # not reported as damaged.
    tokens = re.findall(r"[^\W\d_]+", payload, flags=re.UNICODE)
    isolated = sum(1 for token in tokens if len(token) == 1)
    if len(tokens) >= 12 and isolated >= 8 and isolated / len(tokens) >= 0.35:
        issues.append("Likely OCR gibberish (isolated letters)")
    return " + ".join(dict.fromkeys(issues)) or "None"


class SubtitleCachePending(RuntimeError):
    """Inspection must wait for the scheduled complete-subtitle cache."""


def inspect_extended(path: Path, text_cache: dict | None = None, *, cache_only: bool = False, metadata: dict | None = None) -> list[tuple]:
    """Inspect subtitle health; background indexing may require verified cache."""
    text_cache = text_cache if text_cache is not None else {}
    found = []
    subtitle_index = 0
    metadata = metadata if metadata is not None else probe(path)
    snapshot = None
    if not cache_only:
        try:
            from app.subtitle_cache_worker import text_track_manifest
            signature, tracks = text_track_manifest(path, metadata)
            snapshot = (signature, {(t["source"], t["type_index"], t["external_path"]) for t in tracks})
        except OSError:
            pass
    for stream in metadata.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "unknown")
        if codec in TEXT_SUBTITLE_CODECS:
            cache_key = ("embedded", subtitle_index)
            text = text_cache.get(cache_key)
            if text is None:
                if cache_only:
                    raise SubtitleCachePending(f"Subtitle {subtitle_index + 1} is not cached yet")
                text = cached_subtitle_text(path, "embedded", subtitle_index, snapshot=snapshot)
                if text is None:
                    text = extracted_text(path, f"0:s:{subtitle_index}")
                text_cache[cache_key] = text
            encoding, markup = "UTF-8 (container)", markup_kind(text)
            damage = damage_kind(text)
        else:
            encoding, markup, damage = "Bitmap", "Graphical", "None"
        found.append((str(path), "embedded", subtitle_index, "", codec, encoding, markup, damage))
        subtitle_index += 1
    for item in external_subtitles(path):
        subtitle = Path(item["path"])
        cache_key = ("external", str(subtitle))
        cached = text_cache.get(cache_key)
        if cached is None:
            if cache_only:
                raise SubtitleCachePending(f"External subtitle is not cached yet: {subtitle.name}")
            original_sample, encoding = decode_external(subtitle.read_bytes()[:2_000_000])
            cached = (cached_subtitle_text(path, "external", -1, str(subtitle), snapshot=snapshot) or original_sample, encoding)
            text_cache[cache_key] = cached
        elif isinstance(cached, str):
            cached = (cached, "UTF-8 (cached)") if cache_only else (cached, decode_external(subtitle.read_bytes()[:2_000_000])[1])
        text, encoding = cached
        found.append((str(path), "external", -1, str(subtitle), item.get("codec") or subtitle.suffix.lstrip("."), encoding, markup_kind(text), damage_kind(text)))
    return found


def filter_values() -> dict:
    base = movie_index.movie_stream_filter_values()
    with connection() as db:
        base["subtitle_encodings"] = sorted({row[0] for row in db.execute("SELECT DISTINCT encoding FROM subtitle_extended_index WHERE encoding!=''")}, key=str.casefold)
        base["subtitle_markup"] = sorted({row[0] for row in db.execute("SELECT DISTINCT markup FROM subtitle_extended_index WHERE markup!=''")}, key=str.casefold)
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


@app.get("/api/v51/subtitle-cache-status")
def subtitle_cache_status(path: str) -> dict:
    """Live, source- and checksum-verified per-track status for stream properties."""
    from app.subtitle_cache import valid_cached_keys
    from app.subtitle_cache_worker import TEXT_CODECS, TEXT_SIDECAR_SUFFIXES, text_track_manifest

    media = authorized_import_file(path)
    metadata = probe(media)
    signature, tracks = text_track_manifest(media, metadata)
    eligible = {(track["source"], track["type_index"], track["external_path"]) for track in tracks}
    valid = valid_cached_keys(str(media), signature) & eligible
    result = []
    subtitle_index = 0
    for stream in metadata.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        key = ("embedded", subtitle_index, "")
        result.append({"source": key[0], "type_index": key[1], "external_path": key[2],
                       "cacheable": str(stream.get("codec_name") or "").casefold() in TEXT_CODECS,
                       "cached": key in valid})
        subtitle_index += 1
    for item in external_subtitles(media):
        subtitle = Path(item["path"])
        key = ("external", -1, str(subtitle))
        result.append({"source": key[0], "type_index": key[1], "external_path": key[2],
                       "cacheable": subtitle.suffix.casefold() in TEXT_SIDECAR_SUFFIXES,
                       "cached": key in valid})
    return {"tracks": result}


def strip_html(text: str) -> str:
    return strip_non_color_html(text)


SRT_TIMING_LINE = re.compile(r"^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}")


def subtitle_payload_lines(text: str) -> tuple[str, ...]:
    """Compare cue text while tolerating remuxed timing/sequence formatting."""
    lines = []
    source_lines = text.splitlines()
    for index, line in enumerate(source_lines):
        value = line.strip()
        cue_number = value.isdecimal() and index + 1 < len(source_lines) and bool(SRT_TIMING_LINE.match(source_lines[index + 1]))
        if value and not cue_number and not SRT_TIMING_LINE.match(value):
            lines.append(value)
    return tuple(lines)


def validate_cleaned_srt(expected: str, actual: str) -> None:
    if not actual or has_removable_html(actual):
        raise RuntimeError("Subtitle cleanup output still contains HTML tags or has no text")
    expected_cues = sum(bool(SRT_TIMING_LINE.match(line)) for line in expected.splitlines())
    actual_cues = sum(bool(SRT_TIMING_LINE.match(line)) for line in actual.splitlines())
    if expected_cues != actual_cues or subtitle_payload_lines(expected) != subtitle_payload_lines(actual):
        raise RuntimeError("Subtitle cleanup output changed cue count or dialogue; original retained")


def _publish_cleaned_cache(media: Path, source: str, type_index: int, external_path: str, verified_srt: str) -> None:
    # Media replacement has already passed verification. A cache outage must
    # not turn that completed filesystem edit into a retryable failed job.
    try:
        from app.subtitle_cache_worker import publish_edited_track
        publish_edited_track(media, source, type_index, external_path, verified_srt)
    except Exception as exc:
        logger.warning("subtitle_cache event=edited_track_publish_failed path=%s error=%s", media, str(exc).replace("\n", " ")[:300])


def clean_external_html(subtitle: Path, operation_id: str | None = None, media: Path | None = None) -> bool:
    from app.job_safety import stamp, replace_prepared, output_space
    original = stamp(subtitle)
    text, current = decode_external(subtitle.read_bytes())
    if media is not None and subtitle.suffix.casefold() == ".srt":
        text = cached_subtitle_text(media, "external", -1, str(subtitle)) or text
    if not has_removable_html(text):
        logger.info("change=subtitle_html_cleanup_skipped reason=no_tags file=%s source=external", str(subtitle).replace("\n", "\\n"))
        return False
    codecs = {"UTF-8": "utf-8", "UTF-8 BOM": "utf-8-sig", "UTF-16": "utf-16", "Windows-1252": "cp1252"}
    token = re.sub(r"[^A-Za-z0-9_-]", "", str(operation_id or uuid.uuid4().hex))[-48:]
    temporary = subtitle.with_name(f".{subtitle.name}.vse-{token}.tmp")
    clean = strip_html(text)
    try:
        with output_space(subtitle.parent, original['size'] * 2):
            temporary.write_bytes(clean.encode(codecs[current], errors="replace"))
            verified, _ = decode_external(temporary.read_bytes())
            if subtitle.suffix.casefold() == ".srt":
                validate_cleaned_srt(clean, verified)
            elif not verified or has_removable_html(verified):
                raise RuntimeError("External subtitle cleanup output verification failed")
            os.chmod(temporary, subtitle.stat().st_mode)
            replace_prepared(temporary, subtitle, original)
    finally:
        temporary.unlink(missing_ok=True)
    if media is not None:
        if subtitle.suffix.casefold() == ".srt":
            _publish_cleaned_cache(media, "external", -1, str(subtitle), verified)
        else:
            try:
                from app.subtitle_cache_worker import _extract_track
                normalized = _extract_track(media, {"source": "external", "type_index": -1, "external_path": str(subtitle), "codec": subtitle.suffix[1:].casefold()}).text
                _publish_cleaned_cache(media, "external", -1, str(subtitle), normalized)
            except Exception as exc:
                logger.warning("subtitle_cache event=external_refresh_failed path=%s error=%s", subtitle, str(exc).replace("\n", " ")[:300])
                try:
                    from app.subtitle_cache import invalidate_media
                    invalidate_media(str(media))
                except Exception:
                    logger.warning("subtitle_cache event=external_invalidation_failed path=%s", media)
    return True


def clean_embedded(media: Path, type_index: int, operation_id: str | None = None) -> bool:
    from app.job_safety import stamp, replace_prepared, output_space, run_write_command
    original = stamp(media)
    streams = probe(media).get("streams", [])
    subtitle_globals = [index for index, stream in enumerate(streams) if stream.get("codec_type") == "subtitle"]
    if type_index < 0 or type_index >= len(subtitle_globals):
        raise HTTPException(404, "Subtitle stream was not found")
    selected_global = subtitle_globals[type_index]
    # Cleanup must inspect the whole stream. Some valid subtitles have their
    # first event well after the 15-minute indexing sample.
    text = cached_subtitle_text(media, "embedded", type_index)
    if text is None:
        text = complete_extracted_text(media, f"0:s:{type_index}")
    if not text:
        raise HTTPException(422, "Only text subtitles can have markup removed")
    if not has_removable_html(text):
        # Cleanup requests may have been queued from an older preview/index or
        # the same stream may already have been cleaned. Treat that as complete
        # instead of aborting unrelated edits in the same task.
        logger.info("change=subtitle_html_cleanup_skipped reason=no_tags file=%s stream=subtitle:%d", str(media).replace("\n", "\\n"), type_index)
        return False
    clean = strip_html(text)
    with tempfile.TemporaryDirectory(prefix="vse-subtitle-") as folder:
        subtitle = Path(folder) / "clean.srt"
        subtitle.write_text(clean, encoding="utf-8")
        token = re.sub(r"[^A-Za-z0-9_-]", "", str(operation_id or uuid.uuid4().hex))[-48:]
        temporary = media.with_name(f".{media.stem}.subtitle-clean.vse-{token}{media.suffix}")
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
        # This path is owned by the current task. Remove an artifact left by a
        # previous interrupted attempt, then allow FFmpeg to replace it.
        temporary.unlink(missing_ok=True)
        command.insert(1, "-y")
        try:
            with output_space(media.parent, int(original['size'] * 1.1) + 64 * 1024**2):
                run_write_command(command, media.parent)
                # Verify a readable container with the same stream count before
                # publishing it. A zero-exit encoder alone is not verification.
                if len(probe(temporary).get('streams', [])) != len(streams):
                    raise RuntimeError('Subtitle cleanup output stream verification failed')
                verified = complete_extracted_text(temporary, f"0:s:{type_index}")
                validate_cleaned_srt(clean, verified)
                os.chmod(temporary, media.stat().st_mode)
                replace_prepared(temporary, media, original)
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(422, (getattr(exc, "stderr", None) or "Subtitle cleanup failed")[-1600:]) from exc
        except RuntimeError as exc:
            raise HTTPException(422, str(exc)[:1600]) from exc
        finally:
            temporary.unlink(missing_ok=True)
    _publish_cleaned_cache(media, "embedded", type_index, "", verified)
    return True


@app.post("/api/v51/subtitle-cleanup")
def apply_subtitle_cleanup(request: SubtitleCleanup, operation_id: str | None = None) -> dict:
    from app.v86 import assert_media_editable
    assert_media_editable(request.path)
    media = authorized_import_file(request.path)
    if request.external_path:
        subtitle = checked_external(media, request.external_path)
        changed = clean_external_html(subtitle, operation_id, media)
        target = subtitle.name
    else:
        changed = clean_embedded(media, request.type_index if request.type_index is not None else -1, operation_id)
        target = f"subtitle:{request.type_index}"
    # Legacy movie stream projection tables were removed in the canonical
    # index migration.  Invalidate only the current subtitle index; the task
    # workflow queues the dependent stages after this function returns.
    if changed:
        with connection() as db:
            db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (str(media),))
            db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (str(media),))
        logger.info("change=subtitle_html_removed file=%s target=%s", str(media).replace("\n", "\\n"), target.replace("\n", "\\n"))
    return {"changed": changed, "path": str(media)}
