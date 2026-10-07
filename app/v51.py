from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel

import app.v38 as movie_index
from app.v2 import probe
from app.v5 import external_subtitles
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


def cached_subtitle_text(media: Path, source: str, type_index: int, external_path: str = "", snapshot: tuple | None = None,
                         *, with_details: bool = False, mutation_input: bool = False):
    """Use only source-verified complete text; a miss keeps the old read path."""
    try:
        if snapshot is not None or with_details or mutation_input:
            from app.subtitle_cache import get_valid_track
            if snapshot is None:
                from app.subtitle_cache_worker import text_track_manifest
                signature, tracks = text_track_manifest(media)
                snapshot = (signature, { (t["source"], t["type_index"], t["external_path"]) for t in tracks })
            signature, keys = snapshot
            key = (source, type_index, external_path)
            if key not in keys:
                return None
            track = get_valid_track(str(media), signature, key)
            # Older FFmpeg-decoded caches can have normalized tags or hidden
            # bad source bytes. Keep them readable, but refresh only affected
            # tracks before using their text to replace a media stream.
            if track and mutation_input and track.extraction_revision < 2:
                return None
            return (track if with_details else track.text) if track else None
        from app.subtitle_cache_worker import cached_track_text
        return cached_track_text(media, source, type_index, external_path)
    except Exception as exc:
        logger.warning("subtitle_cache event=read_unavailable path=%s error=%s", media, str(exc).replace("\n", " ")[:240])
        return None


def damage_kind(text: str) -> str:
    """Return conservative SRT corruption markers, not ordinary accented text."""
    from app.subtitle_damage_policy import has_mojibake, language_punctuation_only, isolated_letter_stats, OCR_REASON
    issues = []
    if not text.strip():
        return "Empty subtitle track (no cues)"
    if "\ufffd" in text:
        issues.append("Replacement characters")
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\r\n\t")
    if controls:
        issues.append("Control characters")
    cue_numbers = len(re.findall(r"(?m)^\s*\d+\s*$", text))
    timings = len(re.findall(r"(?m)^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->\s+\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}", text))
    if cue_numbers and not timings:
        issues.append("Malformed SRT timing")
    if has_mojibake(text):
        issues.append("Possible mojibake")
    # Do not classify a subtitle merely because its language is outside the
    # configured detector. Only flag substantial payloads with almost no
    # Unicode letters at all, which is characteristic of broken OCR/decoding.
    payload = re.sub(r"^\s*\d+\s*$|^\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s+-->.*$", " ", text, flags=re.MULTILINE)
    printable = "".join(char for char in payload if char.isprintable() and not char.isspace())
    letters = sum(char.isalpha() for char in printable)
    if len(printable) >= 40 and letters < max(4, len(printable) // 12) and not re.search(r"[♪♫]", payload) and not language_punctuation_only(printable):
        issues.append("No recognizable text")
    # Inspect visible dialogue, not HTML/ASS tokens or letters inside time
    # notation. A high ratio alone is not proof: require fragmented runs too.
    if isolated_letter_stats(text).suspicious:
        issues.append(OCR_REASON)
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
                cached = cached_subtitle_text(path, "embedded", subtitle_index, snapshot=snapshot, with_details=True)
                text = cached.text if cached else None
                if cached:
                    text_cache[("source_encoding", *cache_key)] = cached.source_encoding
                if text is None:
                    text = extracted_text(path, f"0:s:{subtitle_index}")
                text_cache[cache_key] = text
            encoding = text_cache.get(("source_encoding", *cache_key)) or "UTF-8 (container)"
            markup = markup_kind(text)
            damage = damage_kind(text)
            if "inferred" in encoding:
                damage = "Non-UTF-8 source bytes" + (" + " + damage if damage != "None" else "")
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
            saved = cached_subtitle_text(path, "external", -1, str(subtitle), snapshot=snapshot)
            cached = (saved if saved is not None else original_sample, encoding)
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
    """Live source-verified per-track status for stream properties."""
    from app.subtitle_cache import valid_cached_keys
    from app.subtitle_cache_worker import TEXT_CODECS, TEXT_SIDECAR_SUFFIXES, image_track_manifest, text_track_manifest
    from app.subtitle_image_cache import IMAGE_CODECS, cached_image_indexes

    media = authorized_import_file(path)
    metadata = probe(media)
    signature, tracks = text_track_manifest(media, metadata)
    eligible = {(track["source"], track["type_index"], track["external_path"]) for track in tracks}
    valid = valid_cached_keys(str(media), signature) & eligible
    image_signature, image_tracks = image_track_manifest(media, metadata)
    image_eligible = {track["type_index"] for track in image_tracks}
    valid_images = cached_image_indexes(str(media), image_signature) & image_eligible
    result = []
    subtitle_index = 0
    for stream in metadata.get("streams", []):
        if stream.get("codec_type") != "subtitle":
            continue
        key = ("embedded", subtitle_index, "")
        codec_name = str(stream.get("codec_name") or "").casefold()
        result.append({"source": key[0], "type_index": key[1], "external_path": key[2],
                       "cacheable": codec_name in TEXT_CODECS or codec_name in IMAGE_CODECS,
                       "cached": key in valid or subtitle_index in valid_images})
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


def apply_subtitle_cleanups(path: str, requests: list[SubtitleCleanup], operation_id: str | None = None, progress=None) -> dict:
    """One media-level cleanup shared by individual, bulk and editor requests."""
    from app.v86 import assert_media_editable
    from app.subtitle_cleanup import clean_subtitles
    assert_media_editable(path)
    media = authorized_import_file(path)
    if any(Path(request.path).resolve() != media for request in requests):
        raise HTTPException(400, "Subtitle cleanup targets must belong to the same media")
    result = clean_subtitles(media, [request.model_dump() for request in requests], operation_id=operation_id, progress=progress)
    # Legacy movie stream projection tables were removed in the canonical
    # index migration.  Invalidate only the current subtitle index; the task
    # workflow queues the dependent stages after this function returns.
    if result["changed"]:
        with connection() as db:
            db.execute("DELETE FROM subtitle_extended_index WHERE path=?", (str(media),))
            db.execute("DELETE FROM subtitle_extended_media WHERE path=?", (str(media),))
        logger.info("change=subtitle_html_removed file=%s targets=%d", str(media).replace("\n", "\\n"), result["changed_subtitles"])
    return result


@app.post("/api/v51/subtitle-cleanup")
def apply_subtitle_cleanup(request: SubtitleCleanup, operation_id: str | None = None) -> dict:
    return apply_subtitle_cleanups(request.path, [request], operation_id=operation_id)
