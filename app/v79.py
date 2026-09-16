from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v54 as indexes
import app.v65 as tasks
from app.v11 import connection
from app.v13 import media_details_with_ietf
from app.v51 import decode_external, extracted_text
from app.v43 import optimized_media_edit
from app.v5 import SUBTITLE_EXTENSIONS, canonical_language, external_filename_metadata, external_subtitles, plex_language_pair
from app.v78 import app
from app.v7 import ReorderEditRequest


logger = logging.getLogger("videostreamedit")
_legacy_processors = dict(indexes.processors)


class SeasonStreamRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=2000)


class TvShowStatusRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=30000)


class SeasonStreamFilter(BaseModel):
    presence: Literal["have", "not_have"] = "have"
    stream_type: Literal["audio", "subtitle", "external"] | None = None
    stream_types: list[Literal["audio", "subtitle", "external"]] | None = None
    language: str | None = None
    languages: list[str] | None = None
    region: str | None = None
    language_regions: list[str] | None = None
    track_name: str | None = None
    filename_tag: str | None = None


class SeasonStreamBulkEdit(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=2000)
    filters: SeasonStreamFilter
    changed_fields: list[Literal["language", "region", "track_name", "default", "forced", "integrate", "remove"]] = Field(min_length=1)
    language: str = Field(default="", max_length=64)
    region: str = Field(default="", max_length=64)
    track_name: str = Field(default="", max_length=512)
    integrate: bool = False
    remove: bool = False
    default_action: Literal["unchanged", "set", "clear"] = "unchanged"
    forced_action: Literal["unchanged", "set", "clear"] = "unchanged"
    target_keys: list[str] | None = None
    mode: Literal["now", "queue"]


def ensure_tv_stream_index() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS tv_stream_index_media (
                path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tv_stream_index_value (
                path TEXT NOT NULL, stream_type TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT '', region TEXT NOT NULL DEFAULT '',
                track_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS tv_stream_index_value_path ON tv_stream_index_value(path);
            CREATE INDEX IF NOT EXISTS tv_stream_index_value_filter
                ON tv_stream_index_value(stream_type,language,region,track_name);
            CREATE TABLE IF NOT EXISTS tv_stream_index_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS external_subtitle_index (
                media_path TEXT NOT NULL, external_path TEXT NOT NULL,
                codec TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT '',
                region TEXT NOT NULL DEFAULT '', track_name TEXT NOT NULL DEFAULT '',
                forced INTEGER NOT NULL DEFAULT 0, filename_tags TEXT NOT NULL DEFAULT '[]',
                size INTEGER NOT NULL DEFAULT 0, modified_ns INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(media_path,external_path)
            );
            CREATE INDEX IF NOT EXISTS external_subtitle_index_media ON external_subtitle_index(media_path);
            CREATE TABLE IF NOT EXISTS external_sidecar_index_state (
                job TEXT NOT NULL, path TEXT NOT NULL, signature TEXT NOT NULL,
                indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(job,path)
            );
            CREATE TABLE IF NOT EXISTS portuguese_language_detection (
                path TEXT NOT NULL, source TEXT NOT NULL, type_index INTEGER NOT NULL DEFAULT -1,
                external_path TEXT NOT NULL DEFAULT '', metadata_language TEXT NOT NULL DEFAULT '',
                metadata_region TEXT NOT NULL DEFAULT '', detected_language TEXT NOT NULL,
                confidence REAL NOT NULL, evidence TEXT NOT NULL DEFAULT '',
                sdh_label TEXT NOT NULL DEFAULT '', sdh_confidence REAL NOT NULL DEFAULT 0,
                sdh_evidence TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL,
                PRIMARY KEY(path,source,type_index,external_path)
            );
            CREATE INDEX IF NOT EXISTS portuguese_detection_path ON portuguese_language_detection(path);
            CREATE TABLE IF NOT EXISTS language_detection_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portuguese_detection_state (
                path TEXT PRIMARY KEY, signature TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audio_language_detection (
                path TEXT NOT NULL, type_index INTEGER NOT NULL,
                metadata_language TEXT NOT NULL DEFAULT '', metadata_region TEXT NOT NULL DEFAULT '',
                detected_language TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0,
                samples_json TEXT NOT NULL DEFAULT '[]', mismatch INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT NOT NULL,
                PRIMARY KEY(path,type_index)
            );
            CREATE INDEX IF NOT EXISTS audio_language_detection_mismatch ON audio_language_detection(mismatch,confidence);
        """)
        db.execute("INSERT OR IGNORE INTO language_detection_settings(key,value) VALUES('common_languages',?)", (json.dumps(["pt", "pt-BR", "en"], ensure_ascii=False),))
        db.execute("INSERT OR IGNORE INTO language_detection_settings(key,value) VALUES('voice_detection_enabled','1')")
        db.execute("INSERT OR IGNORE INTO language_detection_settings(key,value) VALUES('voice_detection_url','http://language-id:9000')")
        db.execute("INSERT OR IGNORE INTO language_detection_settings(key,value) VALUES('voice_detection_sample_seconds','30')")
        db.execute("INSERT OR IGNORE INTO language_detection_settings(key,value) VALUES('voice_detection_positions','[0.1,0.5,0.9]')")
        db.execute("INSERT OR IGNORE INTO language_detection_settings(key,value) VALUES('incremental_detection_enabled','1')")
        for column, definition in (("sdh_label", "TEXT NOT NULL DEFAULT ''"), ("sdh_confidence", "REAL NOT NULL DEFAULT 0"), ("sdh_evidence", "TEXT NOT NULL DEFAULT ''")):
            try:
                db.execute(f"ALTER TABLE portuguese_language_detection ADD COLUMN {column} {definition}")
            except Exception as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        try:
            db.execute("ALTER TABLE portuguese_detection_state ADD COLUMN detector_version INTEGER NOT NULL DEFAULT 1")
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise


@app.get("/api/v79/language-detection/settings")
def get_language_detection_settings() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='common_languages'").fetchone()
    try:
        values = json.loads(row["value"]) if row else ["pt", "pt-BR", "en"]
    except (TypeError, ValueError, json.JSONDecodeError):
        values = ["pt", "pt-BR", "en"]
    return {"common_languages": values}


class LanguageDetectionSettings(BaseModel):
    common_languages: list[str] = Field(default_factory=lambda: ["pt", "pt-BR", "en"], min_length=1, max_length=30)


def update_language_detection_settings(request: LanguageDetectionSettings) -> dict:
    values = []
    for value in request.common_languages:
        value = str(value).strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value[:16])
    if not values:
        raise HTTPException(400, "At least one common language is required")
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('common_languages',?)", (json.dumps(values, ensure_ascii=False),))
        paths = [row["path"] for row in db.execute("SELECT path FROM plex_media").fetchall()]
    # The setting is part of each detector fingerprint. Queue subtitle
    # inspection incrementally so the new vocabulary is applied without a
    # destructive rebuild, while deduplication prevents duplicate requests.
    try:
        from app.v80 import enqueue_many
        queued = enqueue_many("subtitles", [{"path": path} for path in paths], "Language detection vocabulary changed") if paths else 0
    except Exception as exc:
        queued = 0
        logger.warning("subtitle_inspection event=settings_queue_failed error=%s", str(exc).replace("\n", " ")[-500:])
    logger.info("change=language_detection_common_languages values=%s queued_subtitle_checks=%d", ",".join(values), queued)
    return {"common_languages": values, "queued": queued}


@app.get("/api/v79/language-detection/forced-exclusions")
def get_forced_report_exclusions() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='forced_report_excluded_track_names'").fetchone()
    try:
        values = json.loads(row["value"]) if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    return {"track_names": values}


class ForcedReportExclusions(BaseModel):
    track_names: list[str] = Field(default_factory=list, max_length=200)


@app.put("/api/v79/language-detection/forced-exclusions")
def save_forced_report_exclusions(request: ForcedReportExclusions) -> dict:
    values = []
    for value in request.track_names:
        cleaned = str(value).strip()
        if cleaned and cleaned.casefold() not in {item.casefold() for item in values}:
            values.append(cleaned[:200])
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('forced_report_excluded_track_names',?)", (json.dumps(values, ensure_ascii=False),))
    logger.info("change=forced_report_exclusions_saved count=%d", len(values))
    return {"track_names": values}


class VoiceDetectionSettings(BaseModel):
    enabled: bool = True
    service_url: str = Field(default="http://language-id:9000", max_length=500)
    sample_seconds: int = Field(default=30, ge=10, le=120)
    sample_positions: list[float] = Field(default_factory=lambda: [0.1, 0.5, 0.9], min_length=1, max_length=5)


def voice_detection_settings() -> dict:
    defaults = {"enabled": True, "service_url": "http://language-id:9000", "sample_seconds": 30, "sample_positions": [0.1, 0.5, 0.9]}
    with connection() as db:
        rows = db.execute("SELECT key,value FROM language_detection_settings WHERE key LIKE 'voice_detection_%'").fetchall()
    values = {str(row["key"]): str(row["value"]) for row in rows}
    try: positions = [max(0.0, min(1.0, float(x))) for x in json.loads(values.get("voice_detection_positions", "[0.1,0.5,0.9]"))]
    except (TypeError, ValueError, json.JSONDecodeError): positions = defaults["sample_positions"]
    return {"enabled": values.get("voice_detection_enabled", "1") == "1", "service_url": values.get("voice_detection_url", defaults["service_url"]), "sample_seconds": max(10, min(120, int(values.get("voice_detection_sample_seconds", "30")))), "sample_positions": positions or defaults["sample_positions"]}


@app.get("/api/v79/audio-language-detection/settings")
def get_voice_detection_settings() -> dict:
    return voice_detection_settings()


@app.get("/api/v79/language-detection/queue-settings")
def get_detection_queue_settings() -> dict:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='incremental_detection_enabled'").fetchone()
    return {"incremental_enabled": str(row["value"] if row else "1") == "1"}


class DetectionQueueRequest(BaseModel):
    mode: Literal["full", "incremental", "disable"]


@app.post("/api/v79/language-detection/queue")
def queue_language_detection(request: DetectionQueueRequest) -> dict:
    """Queue subtitle and voice detection without doing media work in the request."""
    from app.v80 import enqueue_many as enqueue_index_many
    from app.v65 import enqueue as enqueue_task
    with connection() as db:
        media = [dict(row) for row in db.execute("SELECT path,title FROM plex_media WHERE kind IN ('movie','episode') ORDER BY path").fetchall()]
        if request.mode == "full":
            db.execute("DELETE FROM portuguese_language_detection")
            db.execute("DELETE FROM portuguese_detection_state")
            db.execute("DELETE FROM audio_language_detection")
        db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES('incremental_detection_enabled',?)", ("1" if request.mode == "incremental" else "0",))
    queued_media = media if request.mode == "full" else []
    subtitle_added = enqueue_index_many("subtitles", queued_media, "Full language detection rebuild") if queued_media else 0
    voice_added = 0
    for item in queued_media:
        task = enqueue_task("audio_language_detection", {"path": item["path"]}, "Voice language detection rebuild", deduplicate=True)
        if task.get("status") == "pending":
            voice_added += 1
    logger.info("language_detection event=queue_requested mode=%s media=%d subtitle_added=%d voice_added=%d", request.mode, len(media), subtitle_added, voice_added)
    return {"mode": request.mode, "media": len(queued_media), "subtitle_queued": subtitle_added, "voice_queued": voice_added, "incremental_enabled": request.mode == "incremental"}


@app.put("/api/v79/audio-language-detection/settings")
def save_voice_detection_settings(request: VoiceDetectionSettings) -> dict:
    url = request.service_url.strip().rstrip("/") or "http://language-id:9000"
    positions = [max(0.0, min(1.0, float(value))) for value in request.sample_positions]
    with connection() as db:
        for key, value in (("voice_detection_enabled", "1" if request.enabled else "0"), ("voice_detection_url", url), ("voice_detection_sample_seconds", str(request.sample_seconds)), ("voice_detection_positions", json.dumps(positions))):
            db.execute("INSERT OR REPLACE INTO language_detection_settings(key,value) VALUES(?,?)", (key, value))
    logger.info("change=voice_detection_settings enabled=%s seconds=%d positions=%s", request.enabled, request.sample_seconds, positions)
    return voice_detection_settings()


class AudioLanguageDetectionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class StreamLanguageDetectionRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    codec_type: Literal["audio", "subtitle"]
    type_index: int = Field(ge=-1, le=1000)
    external: bool = False


@app.post("/api/v79/language-detection/stream")
def detect_stream_language(request: StreamLanguageDetectionRequest) -> dict:
    path = str(Path(request.path).resolve())
    with connection() as db:
        media = db.execute("SELECT kind FROM plex_media WHERE path=?", (path,)).fetchone()
    if not media or media["kind"] not in {"movie", "episode"} or not Path(path).is_file():
        raise HTTPException(404, "Media is not an accessible synchronized Plex item")
    if request.codec_type == "audio":
        try:
            result = tasks.process_audio_language_detection(0, {"path": path})
        except Exception as exc:
            raise HTTPException(422, f"Audio language detection failed: {exc}") from exc
        selected = next((item for item in result.get("results", []) if int(item.get("type_index", -1)) == request.type_index), None)
        return {"status": "completed", "codec_type": "audio", **(selected or {"type_index": request.type_index, "detected_language": "", "confidence": 0})}
    source = "external" if request.external else "embedded"
    with connection() as db:
        row = db.execute("SELECT external_path FROM media_stream_index WHERE path=? AND stream_type='subtitle' AND source=? AND type_index=?", (path, source, request.type_index)).fetchone()
    try:
        if source == "external":
            if not row or not row["external_path"]:
                raise ValueError("External subtitle is not indexed")
            text, _ = decode_external(Path(row["external_path"]).read_bytes()[:2_000_000])
        else:
            text = extracted_text(Path(path), f"0:s:{request.type_index}")
        allowed = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in common_detection_languages()}
        detected, confidence, evidence = detect_common_variant(text, allowed)
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(422, f"Subtitle language detection failed: {exc}") from exc
    return {"status": "completed", "codec_type": "subtitle", "detected_language": detected, "confidence": confidence, "evidence": evidence}


@app.post("/api/v79/audio-language-detection/queue")
def queue_audio_language_detection(request: AudioLanguageDetectionRequest) -> dict:
    path = str(Path(request.path))
    with connection() as db:
        row = db.execute("SELECT kind FROM plex_media WHERE path=?", (path,)).fetchone()
    if not row or row["kind"] not in {"movie", "episode"}:
        raise HTTPException(404, "Media is not in the synchronized Plex catalog")
    from app import v65 as task_queue
    task = task_queue.enqueue("audio_language_detection", {"path": path}, "Detect audio stream languages", deduplicate=True)
    return {"queued": True, "task_id": task["id"], "status": task["status"]}


@app.get("/api/v79/reports/audio-language")
def audio_language_report() -> dict:
    with connection() as db:
        rows = db.execute("""SELECT d.path,d.type_index,d.metadata_language,d.metadata_region,
            d.detected_language,d.confidence,d.samples_json,p.kind,p.title
            FROM audio_language_detection d JOIN plex_media p ON p.path=d.path
            WHERE d.mismatch=1 ORDER BY p.title COLLATE NOCASE,d.path,d.type_index""").fetchall()
    items=[]
    for row in rows:
        item=dict(row)
        try: item["samples"]=json.loads(item.pop("samples_json") or "[]")
        except (TypeError,ValueError,json.JSONDecodeError): item["samples"]=[]
        item["confidence"]=round(float(item["confidence"])*100,1)
        items.append(item)
    return {"items":items,"media_count":len({item["path"] for item in items}),"stream_count":len(items)}


def common_detection_languages() -> list[str]:
    with connection() as db:
        row = db.execute("SELECT value FROM language_detection_settings WHERE key='common_languages'").fetchone()
    try:
        values = json.loads(row["value"]) if row else ["pt", "pt-BR", "en"]
    except (TypeError, ValueError, json.JSONDecodeError):
        values = ["pt", "pt-BR", "en"]
    return [str(value).strip() for value in values if str(value).strip()]


@app.on_event("startup")
def initialize_tv_stream_index() -> None:
    ensure_tv_stream_index()
    with connection() as db:
        version = db.execute("SELECT value FROM tv_stream_index_settings WHERE key='format_version'").fetchone()
        if not version or version["value"] != "2":
            db.execute("DELETE FROM tv_stream_index_value")
            db.execute("DELETE FROM tv_stream_index_media")
            db.execute("INSERT OR REPLACE INTO tv_stream_index_settings(key,value) VALUES('format_version','2')")
            logger.info("tv_season_index event=format_upgraded version=2 external_subtitles=enabled")
        db.execute("DELETE FROM tv_stream_index_value WHERE path NOT IN (SELECT path FROM plex_media WHERE kind='episode')")
        db.execute("DELETE FROM tv_stream_index_media WHERE path NOT IN (SELECT path FROM plex_media WHERE kind='episode')")
        db.execute("DELETE FROM external_subtitle_index WHERE media_path NOT IN (SELECT path FROM plex_media)")
        db.execute("DELETE FROM external_sidecar_index_state WHERE path NOT IN (SELECT path FROM plex_media)")
        db.execute("DELETE FROM portuguese_language_detection WHERE path NOT IN (SELECT path FROM plex_media)")
        db.execute("DELETE FROM portuguese_detection_state WHERE path NOT IN (SELECT path FROM plex_media)")


def external_sidecar_data(path: str, candidates: list[Path] | None = None) -> tuple[list[dict], str]:
    media = Path(path)
    if not media.is_file():
        return [], "missing"
    values = []
    if candidates is None:
        streams = external_subtitles(media)
    else:
        prefix = media.stem.casefold()
        streams = []
        for candidate in candidates:
            candidate_stem = candidate.stem
            folded = candidate_stem.casefold()
            if folded != prefix and not folded.startswith(prefix + "."):
                continue
            suffix = candidate_stem[len(media.stem):].lstrip(".")
            language, region, filename_tags = external_filename_metadata(suffix)
            streams.append({
                "path": str(candidate.resolve()), "name": candidate.name,
                "codec_type": "subtitle", "codec": candidate.suffix.lower().lstrip("."),
                "language": language, "region": region, "title": "",
                "forced": "Forced" in filename_tags, "external": True,
                "filename_tags": filename_tags,
            })
    for stream in streams:
        subtitle = Path(stream["path"])
        try:
            stat = subtitle.stat()
            size, modified_ns = stat.st_size, stat.st_mtime_ns
        except OSError:
            size = modified_ns = 0
        values.append({**stream, "size": size, "modified_ns": modified_ns})
    fingerprint = json.dumps(
        [(item["path"], item["size"], item["modified_ns"]) for item in values],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return values, hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def persist_external_sidecars(job: str, path: str) -> None:
    values, signature = external_sidecar_data(path)
    with connection() as db:
        if job == "core":
            db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (path,))
            db.executemany(
                "INSERT INTO external_subtitle_index(media_path,external_path,codec,language,region,track_name,forced,filename_tags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(path, item["path"], str(item.get("codec") or ""), str(item.get("language") or ""), str(item.get("region") or ""), str(item.get("title") or ""), int(bool(item.get("forced"))), json.dumps(item.get("filename_tags") or [], ensure_ascii=False), item["size"], item["modified_ns"]) for item in values],
            )
        db.execute("INSERT OR REPLACE INTO external_sidecar_index_state(job,path,signature,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)", (job, path, signature))


def pending_external_sidecars(job: str) -> list[dict]:
    ensure_tv_stream_index()
    with connection() as db:
        rows = [dict(row) for row in db.execute("""
            SELECT media.path,media.modified,media.size,media.title,state.signature
              FROM plex_media media
              LEFT JOIN external_sidecar_index_state state ON state.job=? AND state.path=media.path
             ORDER BY media.kind,media.title COLLATE NOCASE,media.path
        """, (job,))]
    pending = []
    directory_candidates: dict[Path, list[Path]] = {}
    for directory in {Path(row["path"]).parent for row in rows}:
        try:
            directory_candidates[directory] = [
                candidate for candidate in sorted(directory.iterdir(), key=lambda value: value.name.casefold())
                if candidate.is_file() and candidate.suffix.lower() in SUBTITLE_EXTENSIONS
            ]
        except OSError:
            directory_candidates[directory] = []
    baselines: list[tuple[dict, list[dict], str]] = []
    for row in rows:
        values, signature = external_sidecar_data(row["path"], directory_candidates[Path(row["path"]).parent])
        if row.get("signature") is None:
            baselines.append((row, values, signature))
        elif row["signature"] != signature:
            row.pop("signature", None)
            pending.append(row)
    if baselines:
        with connection() as db:
            if job == "core":
                for row, values, _ in baselines:
                    db.execute("DELETE FROM external_subtitle_index WHERE media_path=?", (row["path"],))
                    db.executemany(
                        "INSERT INTO external_subtitle_index(media_path,external_path,codec,language,region,track_name,forced,filename_tags,size,modified_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [(row["path"], item["path"], str(item.get("codec") or ""), str(item.get("language") or ""), str(item.get("region") or ""), str(item.get("title") or ""), int(bool(item.get("forced"))), json.dumps(item.get("filename_tags") or [], ensure_ascii=False), item["size"], item["modified_ns"]) for item in values],
                    )
            db.executemany(
                "INSERT OR REPLACE INTO external_sidecar_index_state(job,path,signature,indexed_at) VALUES(?,?,?,CURRENT_TIMESTAMP)",
                [(job, row["path"], signature) for row, _, signature in baselines],
            )
    logger.info("external_sidecar_check event=completed job=%s media=%d baselined=%d pending=%d", job, len(rows), len(baselines), len(pending))
    return pending


def pending_episode_rows() -> list[dict]:
    ensure_tv_stream_index()
    with connection() as db:
        return [dict(row) for row in db.execute("""
            SELECT media.path,media.modified,media.size,media.title
              FROM plex_media media
              LEFT JOIN tv_stream_index_media cached ON cached.path=media.path
             WHERE media.kind='episode'
               AND (cached.path IS NULL OR cached.modified!=media.modified OR cached.size!=media.size)
             ORDER BY media.title COLLATE NOCASE,media.path
        """)]


def inspect_embedded_streams(path: str) -> list[tuple[str, str, str, str]]:
    details = media_details_with_ietf(path)
    embedded = [
        (
            str(stream.get("codec_type") or ""), str(stream.get("language") or "").strip(),
            str(stream.get("region") or "").strip(), str(stream.get("title") or "").strip(),
        )
        for stream in details.get("streams", [])
        if stream.get("codec_type") in {"audio", "subtitle"} and not stream.get("external")
    ]
    external = [
        ("external", str(stream.get("language") or "").strip(), str(stream.get("region") or "").strip(), str(stream.get("title") or "").strip())
        for stream in details.get("external_subtitles", [])
    ]
    return embedded + external


def index_episode(item: dict) -> None:
    ensure_tv_stream_index()
    path = str(item["path"])
    values = inspect_embedded_streams(path)
    with connection() as db:
        db.execute("DELETE FROM tv_stream_index_value WHERE path=?", (path,))
        db.executemany(
            "INSERT INTO tv_stream_index_value(path,stream_type,language,region,track_name) VALUES(?,?,?,?,?)",
            [(path, *value) for value in values],
        )
        db.execute(
            "INSERT OR REPLACE INTO tv_stream_index_media(path,modified,size,indexed_at) VALUES(?,?,?,datetime('now'))",
            (path, int(item["modified"]), int(item["size"])),
        )
    persist_external_sidecars("core", path)


def core_index_with_tv(item: dict) -> None:
    with connection() as db:
        row = db.execute("SELECT kind FROM plex_media WHERE path=?", (item["path"],)).fetchone()
    if row and row["kind"] == "episode":
        index_episode(item)
    else:
        _legacy_processors["core"](item)
        persist_external_sidecars("core", str(item["path"]))
    inspect_portuguese_language(str(item["path"]))


_PT_VARIANT_RESOURCE = Path(__file__).with_name("data") / "pt_variant_lexicon.json"
try:
    _PT_VARIANT_DATA = json.loads(_PT_VARIANT_RESOURCE.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError):
    _PT_VARIANT_DATA = {}
_PT_BR_WORDS = set(_PT_VARIANT_DATA.get("br_words") or {"você", "vocês", "ônibus", "celular", "geladeira", "banheiro", "legal", "a gente", "trem", "arquivo", "tela", "rodoviária"})
_PT_PT_WORDS = set(_PT_VARIANT_DATA.get("pt_words") or {"tu", "comboio", "telemóvel", "frigorífico", "casa de banho", "fixe", "rapariga", "autocarro", "ficheiro", "ecrã", "miúdo", "pequeno-almoço", "bocadinho", "se calhar", "percebido"})
_PT_BR_PATTERNS = tuple(_PT_VARIANT_DATA.get("br_patterns") or ())
_PT_PT_PATTERNS = tuple(_PT_VARIANT_DATA.get("pt_patterns") or ())
# Broad stop-word profiles prevent a repeated English song from outweighing
# an otherwise Portuguese subtitle merely because the old list was tiny.
_PT_COMMON_WORDS = {"o", "a", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "e", "que", "em", "no", "na", "nos", "nas", "para", "por", "com", "sem", "se", "não", "sim", "eu", "ele", "ela", "eles", "elas", "me", "te", "seu", "sua", "seus", "suas", "está", "estão", "foi", "ser", "como", "mais", "mas", "ou", "já", "aqui", "isso", "esse", "essa", "onde", "quando", "porque", "vai", "vou", "tem", "têm"}
_EN_WORDS = {"the", "and", "you", "that", "what", "with", "this", "not", "have", "for", "are", "your", "who", "is", "am", "i", "me", "my", "we", "they", "to", "of", "in", "on", "it", "was", "be", "will", "where", "why", "how", "can", "do", "does", "from", "all", "after", "before", "there", "here", "tell", "must", "never", "someone"}
_PT_BR_RE = re.compile(r"\b(?:estou|estamos|está|estão)\s+(?:fazendo|dizendo|vendo|falando|chegando|entrando|saindo|trabalhando|ligando|tentando)\b", re.I)
_PT_PT_RE = re.compile(r"\b(?:estou|estamos|está|estão)\s+a\s+(?:fazer|dizer|ver|falar|chegar|entrar|sair|trabalhar|ligar|tentar)\b", re.I)
_PT_PT_CONTEXT_RE = re.compile(r"\b(?:percebido|estamos\s+a\s+chegar|estão\s+a\s+chegar|se\s+faz\s+favor|com\s+certeza)\b", re.I)
_PT_BR_CONTEXT_RE = re.compile(r"\b(?:estou|estamos|está|estão)\s+(?:fazendo|dizendo|vendo|falando|chegando|entrando|saindo|trabalhando|ligando|tentando)\b", re.I)

def _resource_pattern_count(patterns: tuple[str, ...], value: str) -> int:
    return sum(len(re.findall(r"(?<!\w)" + re.escape(pattern.casefold()) + r"(?!\w)", value)) for pattern in patterns)


def detect_common_variant(text: str, allowed_languages: set[str] | None = None) -> tuple[str, float, str]:
    allowed = {str(value).casefold().replace("_", "-").split("-", 1)[0] for value in (allowed_languages or {"pt", "en"})}
    normalized = re.sub(r"\s+", " ", text.casefold())

    def term_count(words: set[str]) -> int:
        # Match complete words/phrases only. Raw substring counting makes the
        # PT-PT marker "tu" match inside unrelated words such as "tudo".
        return sum(len(re.findall(r"(?<!\w)" + re.escape(word) + r"(?!\w)", normalized)) for word in words)

    # Establish the dominant base language first. Regional markers are only
    # considered after Portuguese wins, so a short English song or quotation
    # cannot change the classification of an otherwise Portuguese subtitle.
    pt_common = term_count(_PT_COMMON_WORDS) if "pt" in allowed else 0
    br = (term_count(_PT_BR_WORDS) + _resource_pattern_count(_PT_BR_PATTERNS, normalized) * 2 + 2 * len(_PT_BR_RE.findall(normalized)) + 2 * len(_PT_BR_CONTEXT_RE.findall(normalized))) if "pt" in allowed else 0
    pt = (term_count(_PT_PT_WORDS) + _resource_pattern_count(_PT_PT_PATTERNS, normalized) * 2 + 2 * len(_PT_PT_RE.findall(normalized)) + 2 * len(_PT_PT_CONTEXT_RE.findall(normalized))) if "pt" in allowed else 0
    en = term_count(_EN_WORDS) if "en" in allowed else 0
    portuguese = pt_common + br + pt
    if portuguese == 0 and en < 3:
        return "", 0.0, ""
    if en > portuguese * 2.00 and en >= 8:
        detected, score, total = "en", en, en + portuguese
    elif portuguese >= 6 and portuguese >= en * 1.20 and (pt >= 2 or br >= 2):
        # Ambiguous Portuguese is deliberately ignored instead of producing a
        # misleading PT-BR/PT-PT mismatch report.
        if br >= 2 and br > pt:
            detected, score, total = "pt-BR", portuguese, portuguese + en
        elif pt >= 2 and pt > br:
            detected, score, total = "pt-PT", portuguese, portuguese + en
        else:
            return "", 0.0, ""
    else:
        return "", 0.0, ""
    dominance = score / max(total, 1)
    evidence_strength = min(score / 12, 1.0)
    confidence = min(0.99, 0.60 + max(0.0, dominance - 0.50) * 0.40 * evidence_strength)
    if confidence < 0.60:
        return "", 0.0, ""
    vocabulary = _PT_BR_WORDS if detected == "pt-BR" else _PT_PT_WORDS if detected == "pt-PT" else _EN_WORDS
    evidence = ", ".join(sorted(vocabulary, key=lambda word: (-normalized.count(word), word))[:3])
    return detected, confidence, evidence


_SDH_SOUND_RE = re.compile(r"(?:\[[^\]]{1,120}\]|\([^\)]{1,120}\)|♪|♫|\b(?:music|singing|song|laughs?|crying|sobbing|sighs?|gasps?|door|phone|telephone|alarm|applause|inaudible|música|cantando|risos?|choro|suspiro|porta|telefone|alarme|aplausos|inaudível)\b)", re.I)
_SDH_SPEAKER_RE = re.compile(r"(?:^|\n)\s*(?:\[[^\]]{1,60}\]|[A-ZÀ-Ý][A-ZÀ-Ý .'-]{2,}\s*:)", re.M)

def analyze_sdh(text: str) -> tuple[str, float, str]:
    normalized = re.sub(r"\s+", " ", text or "")
    if not normalized.strip():
        return "Cannot evaluate", 0.0, "No readable subtitle text"
    cues = max(1, len(re.findall(r"-->[^\n]*", text or "")))
    sound_hits = len(_SDH_SOUND_RE.findall(normalized))
    speaker_hits = len(_SDH_SPEAKER_RE.findall(text or ""))
    music_hits = len(re.findall(r"[♪♫]|\b(?:music|song|música|canção)\b", normalized, re.I))
    score = min(0.99, sound_hits / max(cues * 0.18, 1) * 0.45 + speaker_hits / max(cues * 0.12, 1) * 0.35 + music_hits / max(cues * 0.08, 1) * 0.20)
    if score >= 0.72:
        label = "Likely SDH"
    elif score <= 0.18 and sound_hits == 0 and speaker_hits == 0:
        label = "Likely dialogue-only"
    else:
        label = "Uncertain"
    evidence = []
    if sound_hits: evidence.append(f"{sound_hits} sound/description cue(s)")
    if speaker_hits: evidence.append(f"{speaker_hits} speaker label(s)")
    if music_hits: evidence.append(f"{music_hits} music cue(s)")
    return label, round(score, 3), ", ".join(evidence) or "No distinctive SDH markers"


def inspect_portuguese_language(path: str) -> None:
    media = Path(path)
    if not media.is_file():
        return
    stat = media.stat()
    configured = common_detection_languages()
    bases = {value.casefold().split("-", 1)[0].split("_", 1)[0] for value in configured}
    sidecars = []
    for subtitle in external_subtitles(media):
        try:
            subtitle_stat = Path(subtitle["path"]).stat()
            sidecars.append((subtitle["path"], subtitle_stat.st_size, subtitle_stat.st_mtime_ns))
        except OSError:
            continue
    signature = hashlib.sha256(json.dumps(["detector-v8-sdh", stat.st_size, stat.st_mtime_ns, configured, sidecars], ensure_ascii=False).encode()).hexdigest()
    with connection() as db:
        previous = db.execute("SELECT signature FROM portuguese_detection_state WHERE path=?", (path,)).fetchone()
        if previous and previous["signature"] == signature:
            return
        streams = db.execute(
            "SELECT source,type_index,external_path,language,region FROM media_stream_index "
            "WHERE path=? AND stream_type IN ('subtitle','external')", (path,)
        ).fetchall()
    results = []
    for stream in streams:
        metadata_language = str(stream["language"] or "").strip().casefold()
        if metadata_language and metadata_language not in bases and metadata_language != "und":
            continue
        try:
            if stream["source"] == "external":
                raw = Path(stream["external_path"]).read_bytes()[:2_000_000]
                text, _ = decode_external(raw)
            else:
                text = extracted_text(media, f"0:s:{int(stream['type_index'])}")
            detected, confidence, evidence = detect_common_variant(text, bases)
            sdh_label, sdh_confidence, sdh_evidence = analyze_sdh(text)
            if not detected or confidence <= 0.60:
                continue
            expected = "pt-BR" if metadata_language == "pt" and str(stream["region"] or "").upper() == "BR" else "pt-PT" if metadata_language == "pt" else "en" if metadata_language == "en" else "und"
            if metadata_language in {"", "und"} or detected.casefold() != expected.casefold():
                results.append((path, str(stream["source"]), int(stream["type_index"]), str(stream["external_path"] or ""), str(stream["language"] or ""), str(stream["region"] or ""), detected, confidence, evidence, sdh_label, sdh_confidence, sdh_evidence))
        except (OSError, ValueError, TypeError):
            continue
    with connection() as db:
        db.execute("DELETE FROM portuguese_language_detection WHERE path=?", (path,))
        db.executemany("INSERT INTO portuguese_language_detection(path,source,type_index,external_path,metadata_language,metadata_region,detected_language,confidence,evidence,sdh_label,sdh_confidence,sdh_evidence,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)", results)
        db.execute("INSERT OR REPLACE INTO portuguese_detection_state(path,signature,detector_version,checked_at) VALUES(?,?,7,CURRENT_TIMESTAMP)", (path, signature))

def subtitle_index_with_sidecars(item: dict) -> None:
    _legacy_processors["subtitles"](item)
    path = str(item["path"])
    persist_external_sidecars("subtitles", path)
    inspect_portuguese_language(path)


def preview_index_with_sidecars(item: dict) -> None:
    _legacy_processors["previews"](item)
    persist_external_sidecars("previews", str(item["path"]))


indexes.processors["core"] = core_index_with_tv
indexes.processors["subtitles"] = subtitle_index_with_sidecars
indexes.processors["previews"] = preview_index_with_sidecars


def selected_episode_rows(paths: list[str]) -> list[dict]:
    unique = list(dict.fromkeys(paths))
    found = []
    with connection() as db:
        for start in range(0, len(unique), 800):
            group = unique[start:start + 800]
            placeholders = ",".join("?" for _ in group)
            rows = db.execute(
                f"SELECT path,modified,size,title FROM plex_media WHERE kind='episode' AND path IN ({placeholders})",
                group,
            ).fetchall()
            found.extend(dict(row) for row in rows)
    if {item["path"] for item in found} != set(unique):
        raise HTTPException(409, "The selected season changed. Refresh TV Shows and try again")
    return found


def _queue_recoverable_stale_indexes(paths: set[str]) -> set[str]:
    """Queue accessible stale files without retrying terminal failures forever."""
    if not paths:
        return set()
    from app.v80 import enqueue
    with connection() as db:
        active = {row["path"] for row in db.execute("SELECT path FROM index_task_queue WHERE job=\"core\" AND status IN (\"pending\",\"running\")").fetchall()}
        failed = {row["path"] for row in db.execute("SELECT path FROM index_task_queue WHERE job=\"core\" AND status=\"failed\"").fetchall()}
    queued = set()
    for path in paths:
        if path in active or path in failed or not Path(path).is_file():
            continue
        if enqueue("core", path, "Automatic stale fingerprint reconciliation"):
            queued.add(path)
    return queued

@app.post("/api/v79/tv/show-status")
def tv_show_status(request: TvShowStatusRequest) -> dict:
    paths = list(dict.fromkeys(request.paths))
    placeholders = ",".join("?" for _ in paths)
    change_paths: set[str] = set()
    index_paths: set[str] = set()
    with connection() as db:
        change_paths = {row["path"] for row in db.execute(
            f"SELECT marker.path FROM media_change_request marker JOIN task_queue task ON task.id=marker.task_id WHERE marker.path IN ({placeholders}) AND task.status IN ('pending','running')", paths
        ).fetchall()}
        index_paths = {row["path"] for row in db.execute(
            f"SELECT DISTINCT path FROM index_task_queue WHERE path IN ({placeholders}) AND status IN ('pending','running','failed')", paths
        ).fetchall()}
        indexed = {row["path"]: (row["modified_ns"], row["size"]) for row in db.execute(
            f"SELECT path,modified_ns,size FROM media_stream_index_state WHERE path IN ({placeholders})", paths
        ).fetchall()}
    stale_paths = set()
    for path in paths:
        try:
            stat = Path(path).stat()
            saved = indexed.get(path)
            if not saved or int(saved[0] or 0) != int(stat.st_mtime_ns) or int(saved[1] or 0) != int(stat.st_size):
                stale_paths.add(path)
        except OSError:
            stale_paths.add(path)
    auto_queued = _queue_recoverable_stale_indexes(stale_paths)
    if auto_queued:
        index_paths.update(auto_queued)
    reasons = []
    if change_paths: reasons.append("changes queued or processing")
    if index_paths: reasons.append("index update queued or processing")
    if stale_paths: reasons.append("index needs updating")
    return {"active": bool(reasons), "reasons": reasons, "changes": len(change_paths), "indexing": len(index_paths), "stale": len(stale_paths)}


@app.post("/api/v79/tv/season-stream-values")
def season_stream_values(request: SeasonStreamRequest) -> dict:
    ensure_tv_stream_index()
    items = selected_episode_rows(request.paths)
    pending = []
    # Use the unified index fingerprint (filesystem mtime/size).  The legacy
    # TV table stores Plex catalog timestamps, which do not change when stream
    # metadata is edited and caused every season expansion to re-scan its files.
    with connection() as db:
        cached = {
            row["path"]: row for start in range(0, len(items), 800)
            for row in db.execute(
                f"SELECT path,modified_ns,size FROM media_stream_index_state WHERE path IN ({','.join('?' for _ in items[start:start + 800])})",
                [item["path"] for item in items[start:start + 800]],
            ).fetchall()
        }
    for item in items:
        old = cached.get(item["path"])
        try:
            stat = Path(item["path"]).stat()
            current = (int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            current = None
        if not old or current is None or (int(old["modified_ns"]), int(old["size"])) != current:
            pending.append(item)
    errors = []
    for number, item in enumerate(pending, 1):
        try:
            index_episode(item)
        except Exception as exc:
            errors.append({"path": item["path"], "error": str(getattr(exc, "detail", exc))[-500:]})
        if number == 1 or number == len(pending) or number % 10 == 0:
            logger.info("tv_season_index event=progress completed=%d total=%d errors=%d", number, len(pending), len(errors))
    paths = [item["path"] for item in items]
    values = []
    with connection() as db:
        for start in range(0, len(paths), 800):
            group = paths[start:start + 800]
            rows = db.execute(
                f"SELECT path,stream_type,language,region,track_name,filename_tags FROM media_stream_index WHERE path IN ({','.join('?' for _ in group)})",
                group,
            ).fetchall()
            for row in rows:
                value = dict(row)
                value["language"] = comparable_language(value["language"])
                try:
                    value["filename_tags"] = json.loads(value["filename_tags"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    value["filename_tags"] = []
                values.append(value)
    logger.info("tv_season_index event=ready episodes=%d inspected=%d values=%d errors=%d", len(items), len(pending), len(values), len(errors))
    return {"values": values, "episodes": len(items), "inspected": len(pending), "errors": errors}


def comparable_language(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return plex_language_pair(normalized, "")[0]


def region_matches(stream: dict, expected: str | None) -> bool:
    if expected is None:
        return True
    actual = str(stream.get("region") or "").strip().upper()
    wanted = str(expected).strip().upper()
    return actual == wanted


def filter_matches(stream: dict, filters: SeasonStreamFilter) -> bool:
    language_region = f"{comparable_language(stream.get('language'))}|{str(stream.get('region') or '').strip().upper()}"
    selected_pairs = filters.language_regions
    pair_match = not selected_pairs or language_region in {
        f"{comparable_language(value.split('|', 1)[0])}|{value.split('|', 1)[1].strip().upper()}"
        for value in selected_pairs if '|' in value
    }
    return (
        ((filters.stream_types is not None and stream.get("codec_type") in filters.stream_types) or (filters.stream_types is None and (filters.stream_type is None or stream.get("codec_type") == filters.stream_type)))
        and pair_match
        and (filters.language_regions is not None or (filters.languages is not None and comparable_language(stream.get("language")) in {comparable_language(value) for value in filters.languages}) or (filters.languages is None and (filters.language is None or comparable_language(stream.get("language")) == comparable_language(filters.language))))
        and (filters.language_regions is not None or region_matches(stream, filters.region))
        and (filters.track_name is None or str(stream.get("title") or "").strip() == filters.track_name)
        and (filters.filename_tag is None or filters.filename_tag in (stream.get("filename_tags") or []))
    )


def episode_bulk_edit(path: str, request: SeasonStreamBulkEdit) -> tuple[dict, int]:
    details = media_details_with_ietf(path)
    tracks, external_changes, order, remove = [], [], [], []
    # Preserve tag state unless this request actually changes it. This lets
    # bulk preflight discard media that is already compliant.
    tags = {"default_audio": "__preserve__", "forced_audio": "__preserve__", "default_subtitle": "__preserve__", "forced_subtitle": "__preserve__"}
    current_tags = {"default_audio": None, "forced_audio": None, "default_subtitle": None, "forced_subtitle": None}
    matches = 0
    matched_keys: list[str] = []
    changed = set(request.changed_fields)
    targets = set(request.target_keys) if request.target_keys is not None else None
    for stream in details["streams"]:
        stream_type = stream["codec_type"]
        type_index = int(stream["type_index"])
        key = f"embedded:{stream_type}:{type_index}"
        order.append({"source": "embedded", "codec_type": stream_type, "type_index": type_index})
        if stream.get("default"):
            current_tags[f"default_{stream_type}"] = key
        if stream.get("forced"):
            current_tags[f"forced_{stream_type}"] = key
        if targets is not None and key not in targets:
            continue
        if targets is None and not filter_matches(stream, request.filters):
            continue
        matched_keys.append(key)
        if request.remove:
            remove.append(key)
            matches += 1
            continue
        update = {"codec_type": stream_type, "type_index": type_index}
        if "language" in changed or "region" in changed:
            update["language"] = request.language if "language" in changed else str(stream.get("language") or "")
            update["region"] = request.region if "region" in changed else str(stream.get("region") or "")
        if "track_name" in changed:
            update["title"] = request.track_name
        if any(update.get(field) != str(stream.get(source) or "") for field, source in (("language", "language"), ("region", "region"), ("title", "title")) if field in update):
            tracks.append(update)
        matches += 1
    for stream in details.get("external_subtitles", []):
        external_path = str(stream["path"])
        key = f"external:{external_path}"
        order.append({"source": "external", "codec_type": "subtitle", "path": external_path})
        comparable = {**stream, "codec_type": "external"}
        if targets is not None and key not in targets:
            continue
        if targets is None and not filter_matches(comparable, request.filters):
            continue
        if request.remove:
            remove.append(key)
            external_changes.append({
                "path": external_path, "embed": False,
                "language": str(stream.get("language") or "und"),
                "region": str(stream.get("region") or ""),
                "title": str(stream.get("title") or ""),
                "forced": bool(stream.get("forced")),
            })
        elif request.integrate:
            external_changes.append({
                "path": external_path, "embed": True,
                "language": request.language if "language" in changed else str(stream.get("language") or "und"),
                "region": request.region if "region" in changed else str(stream.get("region") or ""),
                "title": request.track_name if "track_name" in changed else str(stream.get("title") or ""),
                "forced": bool(stream.get("forced")),
            })
        matches += 1
    # For set/clear requests, the last matching stream of each type wins,
    # matching the stream-edit ordering rule.
    if request.default_action != "unchanged":
        for stream_type in ("audio", "subtitle"):
            matches_for_type = [key for key in matched_keys if key.startswith(f"embedded:{stream_type}:")]
            desired = f"embedded:{stream_type}:{matches_for_type[-1].rsplit(":", 1)[-1]}" if request.default_action == "set" and matches_for_type else None
            if current_tags[f"default_{stream_type}"] != desired:
                tags[f"default_{stream_type}"] = desired
        if request.filters.stream_type in {"audio", "subtitle"}:
            tags[f"default_{"subtitle" if request.filters.stream_type == "audio" else "audio"}"] = "__preserve__"
    if request.forced_action != "unchanged":
        for stream_type in ("audio", "subtitle"):
            matches_for_type = [key for key in matched_keys if key.startswith(f"embedded:{stream_type}:")]
            desired = f"embedded:{stream_type}:{matches_for_type[-1].rsplit(":", 1)[-1]}" if request.forced_action == "set" and matches_for_type else None
            if current_tags[f"forced_{stream_type}"] != desired:
                tags[f"forced_{stream_type}"] = desired
        if request.filters.stream_type in {"audio", "subtitle"}:
            tags[f"forced_{"subtitle" if request.filters.stream_type == "audio" else "audio"}"] = "__preserve__"
    return {"path": path, "tracks": tracks, "external_subtitles": external_changes, "order": order, **tags, "remove": remove}, matches


def edit_has_effective_changes(edit: dict) -> bool:
    return bool(edit.get("tracks") or edit.get("external_subtitles") or edit.get("remove") or any(edit.get(name) != "__preserve__" for name in ("default_audio", "forced_audio", "default_subtitle", "forced_subtitle")))


def process_tv_filtered_stream_edit(task_id: int, payload: dict) -> dict:
    path = str(payload["path"])
    request_data = dict(payload["request"])
    request_data["paths"] = [path]
    request_data["mode"] = "now"
    request = SeasonStreamBulkEdit.model_validate(request_data)
    episode = (re.search(r"(?:^|[^A-Za-z])(S\d{1,2}E\d{1,2})(?:[^A-Za-z]|$)", path, re.I) or [None, ""])[1].upper()
    prefix = f"Processing episode {episode} · " if episode else "Processing media · "
    tasks.update_progress(task_id, 0, 2, prefix + "Checking current streams")
    edit, matched = episode_bulk_edit(path, request)
    if not matched:
        tasks.update_progress(task_id, 2, 2, prefix + "No matching streams remain; no change needed")
        return {"path": path, "streams": 0, "skipped": True, "reason": "No current stream matches the queued filter"}
    if not edit_has_effective_changes(edit):
        tasks.update_progress(task_id, 2, 2, prefix + "Already compliant; no change needed")
        return {"path": path, "streams": matched, "skipped": True, "reason": "Media already has the requested values"}
    tasks.update_progress(task_id, 1, 2, prefix + f"Applying changes to {matched} matching stream(s)")
    result = optimized_media_edit(ReorderEditRequest.model_validate(edit))
    from app.v80 import request_media_indexes
    reindex = ["core", "previews"] if request.filters.stream_type == "external" or request.remove else ["core"]
    request_media_indexes(path, reindex, "Queued TV filtered stream edit completed")
    tasks.update_progress(task_id, 2, 2, prefix + "Stream changes applied")
    return {**result, "path": path, "streams": matched}


def indexed_target_keys(paths: list[str], filters: SeasonStreamFilter) -> dict[str, list[str]]:
    result = {path: [] for path in paths}
    with connection() as db:
        for start in range(0, len(paths), 800):
            group = paths[start:start + 800]
            rows = db.execute(
                f"SELECT path,source,stream_type,type_index,external_path,language,region,track_name,filename_tags FROM media_stream_index WHERE path IN ({', '.join('?' for _ in group)})",
                group,
            ).fetchall()
            for row in rows:
                value = dict(row)
                try:
                    value["filename_tags"] = json.loads(value["filename_tags"] or "[]")
                except (TypeError, json.JSONDecodeError):
                    value["filename_tags"] = []
                stream = {"codec_type": value["stream_type"], "language": value["language"], "region": value["region"], "title": value["track_name"], "filename_tags": value["filename_tags"]}
                if not filter_matches(stream, filters):
                    continue
                key = f"external:{value['external_path']}" if value["source"] == "external" else f"embedded:{value['stream_type']}:{value['type_index']}"
                result[value["path"]].append(key)
    return result


def enqueue_tv_filtered_edits(paths: list[str], request: SeasonStreamBulkEdit) -> tuple[int, list[int]]:
    template = request.model_dump(exclude={"paths", "mode"})
    targets = indexed_target_keys(paths, request.filters)
    now = tasks.utc_now()
    task_ids: list[int] = []
    with connection() as db:
        for path in paths:
            if not targets[path]:
                continue
            per_media = {**template, "target_keys": targets[path]}
            task_type = 'tv_filtered_stream_edit_now' if request.mode == 'now' else 'tv_filtered_stream_edit'
            payload_data = tasks.attach_media_signature(task_type, {"path": path, "request": per_media})
            payload = json.dumps(payload_data, ensure_ascii=False, separators=(",", ":"))
            cursor = db.execute(
                "INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES(?,?,?,'pending','Waiting',?,?)",
                (task_type, f"TV filtered stream edit · {Path(path).name}", payload, now, now),
            )
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            task_ids.append(int(cursor.lastrowid))
    from app.v80 import invalidate_language_detections
    invalidate_language_detections([path for path in paths if targets.get(path)])
    tasks.wake_queue()
    logger.info("tv_stream_bulk_edit event=batch_queued media=%d first_id=%s last_id=%s", len(task_ids), task_ids[0] if task_ids else "none", task_ids[-1] if task_ids else "none")
    return len(task_ids), task_ids


def process_tv_bulk_preflight(task_id: int, payload: dict) -> dict:
    request_data = dict(payload.get("request") or {})
    paths = list(dict.fromkeys(payload.get("paths") or []))
    request_data.update({"paths": paths, "mode": "now"})
    request = SeasonStreamBulkEdit.model_validate(request_data)
    targets = indexed_target_keys(paths, request.filters)
    candidates: list[tuple[str, dict]] = []
    skipped = 0
    task_type = "tv_filtered_stream_edit_now" if str(payload.get("mode") or "queue") == "now" else "tv_filtered_stream_edit"
    tasks.update_progress(task_id, 0, max(1, len(paths)), "Checking bulk changes")
    for number, path in enumerate(paths, 1):
        if targets.get(path):
            per_media = {**request.model_dump(exclude={"paths", "mode"}), "target_keys": targets[path]}
            try:
                preview, matched = episode_bulk_edit(path, SeasonStreamBulkEdit.model_validate({**per_media, "paths": [path], "mode": "now"}))
                if matched and edit_has_effective_changes(preview):
                    candidates.append((path, per_media))
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning("tv_stream_bulk_preflight event=media_failed path=%s error=%s", path, str(exc).replace("\n", " ")[-300:])
                skipped += 1
        else:
            skipped += 1
        tasks.update_progress(task_id, number, max(1, len(paths)), f"Checked {number} of {len(paths)} media")
    child_ids: list[int] = []
    now = tasks.utc_now()
    with connection() as db:
        for path, per_media in candidates:
            signed = tasks.attach_media_signature(task_type, {"path": path, "request": per_media})
            cursor = db.execute("INSERT INTO task_queue(task_type,label,payload_json,status,progress_message,created_at,updated_at) VALUES(?,?,?,'pending','Waiting',?,?)", (task_type, f"TV filtered stream edit · {Path(path).name}", json.dumps(signed, ensure_ascii=False, separators=(",", ":")), now, now))
            db.execute("INSERT OR REPLACE INTO media_change_request(task_id,path,requested_at) VALUES(?,?,?)", (cursor.lastrowid, path, now))
            child_ids.append(int(cursor.lastrowid))
    from app.v80 import invalidate_language_detections
    if child_ids:
        invalidate_language_detections([path for path, _ in candidates])
    tasks.wake_queue()
    logger.info("tv_stream_bulk_preflight event=completed checked=%d queued=%d skipped=%d", len(paths), len(child_ids), skipped)
    return {"queued": len(child_ids), "skipped": skipped, "task_ids": child_ids}


tasks.TASK_HANDLERS["tv_filtered_stream_edit"] = process_tv_filtered_stream_edit
tasks.TASK_HANDLERS["tv_filtered_stream_edit_now"] = process_tv_filtered_stream_edit
tasks.TASK_HANDLERS["tv_bulk_preflight"] = process_tv_bulk_preflight

@app.post("/api/v79/tv/season-stream-bulk-edit")
def season_stream_bulk_edit(request: SeasonStreamBulkEdit) -> dict:
    request.paths = list(dict.fromkeys(request.paths))
    selected_episode_rows(request.paths)
    changed = set(request.changed_fields)
    if request.filters.stream_type == "external":
        if request.integrate and request.remove:
            raise HTTPException(400, "External subtitles cannot be integrated and removed in the same operation")
        if changed.intersection({"language", "region", "track_name"}) and not request.integrate:
            raise HTTPException(400, "Integrate must be selected to save properties on external subtitles")
        if not request.integrate and not request.remove:
            raise HTTPException(400, "Select Integrate or Remove for matching external subtitles")
    elif request.integrate:
        raise HTTPException(400, "Integrate is available only for external subtitles")
    if request.remove and changed.intersection({"language", "region", "track_name"}):
        raise HTTPException(400, "Remove cannot be combined with metadata changes")
    if "track_name" in changed and request.filters.language is None and not request.filters.language_regions:
        raise HTTPException(400, "Select a language and region before changing track names in bulk")
    if request.mode == "queue":
        preflight = tasks.enqueue("tv_bulk_preflight", {"paths": request.paths, "request": request.model_dump(exclude={"paths", "mode"}), "mode": "queue"}, "Preflight TV bulk stream change")
        return {"mode": "queue", "queued": 1, "preflight": True, "task_ids": [preflight["id"]], "applied": 0, "streams": 0, "skipped": [], "failed": []}
    # Immediate bulk edits use the same per-media workers as queued edits so the
    # splash can report the actual episode and aggregate percentage.
    queued, task_ids = enqueue_tv_filtered_edits(request.paths, request)
    if not queued:
        raise HTTPException(409, "No indexed streams match the selected values; refresh the filters")
    return {"mode": "now", "queued": queued, "task_ids": task_ids, "applied": 0, "streams": 0, "skipped": [], "failed": []}
