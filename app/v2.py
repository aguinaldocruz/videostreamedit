from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
DB_PATH = CONFIG_DIR / "videostreamedit.db"
STATIC_DIR = Path(__file__).parent / "static"
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts"}
BLOCKED_PATHS = {Path("/proc"), Path("/sys"), Path("/dev"), CONFIG_DIR.resolve()}
TYPE_SPECIFIER = {"video": "v", "audio": "a", "subtitle": "s"}
SEASON_PATTERN = re.compile(r"(?:season|series|s)[ ._-]*(\d+)", re.IGNORECASE)

app = FastAPI(title="VideoStreamEdit", version="0.2.0")


class RootCreate(BaseModel):
    kind: Literal["movies", "tv"]
    path: str = Field(min_length=1)
    name: str | None = None


class BatchProbeRequest(BaseModel):
    paths: list[str] = Field(min_length=1)


class StreamUpdate(BaseModel):
    codec_type: Literal["video", "audio", "subtitle"]
    type_index: int = Field(ge=0)
    language: str | None = None
    region: str | None = None
    title: str | None = None


class EditRequest(BaseModel):
    paths: list[str] = Field(min_length=1)
    streams: list[StreamUpdate] = Field(min_length=1)


def connection() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def initialize() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with connection() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS library_roots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL CHECK(kind IN ('movies', 'tv')),
                path TEXT NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(kind, path)
            )
        """)


@app.on_event("startup")
def startup() -> None:
    initialize()


def resolve_existing(value: str) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, "Path does not exist or cannot be accessed") from exc


def is_blocked(path: Path) -> bool:
    return any(path == blocked or blocked in path.parents for blocked in BLOCKED_PATHS)


def roots(kind: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT id, kind, path, name FROM library_roots"
    parameters: tuple = ()
    if kind:
        query += " WHERE kind = ?"
        parameters = (kind,)
    query += " ORDER BY name COLLATE NOCASE"
    with connection() as db:
        return db.execute(query, parameters).fetchall()


def authorized_file(value: str) -> Path:
    path = resolve_existing(value)
    if not path.is_file():
        raise HTTPException(400, "Media path is not a file")
    if not any(Path(row["path"]) in path.parents for row in roots()):
        raise HTTPException(403, "Media file is outside configured library roots")
    return path


def media_files(kind: str) -> list[dict]:
    found = []
    for root in roots(kind):
        root_path = Path(root["path"])
        if not root_path.is_dir():
            continue
        try:
            for path in root_path.rglob("*"):
                if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                    stat = path.stat()
                    found.append({
                        "path": str(path), "name": path.name,
                        "relative_path": str(path.relative_to(root_path)),
                        "parts": path.relative_to(root_path).parts,
                        "root_id": root["id"], "root_name": root["name"],
                        "size": stat.st_size, "modified": int(stat.st_mtime),
                    })
        except PermissionError:
            continue
    return found


def season_sort(name: str) -> tuple[int, int | str]:
    match = SEASON_PATTERN.search(name)
    return (0, int(match.group(1))) if match else (1, name.casefold())


def probe(path: Path) -> dict:
    command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=True)
        return json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise HTTPException(503, "ffprobe is not installed") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        message = getattr(exc, "stderr", None) or "Could not inspect media file"
        raise HTTPException(422, message[-1200:]) from exc


def split_language(value: str) -> tuple[str, str]:
    value = value.strip()
    match = re.match(r"^([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}|\d{3}))?$", value)
    if match:
        return match.group(1).lower(), (match.group(2) or "").upper()
    return value, ""


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "v2.html")


@app.get("/app.css")
def stylesheet() -> FileResponse:
    return FileResponse(STATIC_DIR / "v2.css", media_type="text/css")


@app.get("/app.js")
def javascript() -> FileResponse:
    return FileResponse(STATIC_DIR / "v2.js", media_type="text/javascript")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/browse")
def browse(path: str = Query(default="/")) -> dict:
    directory = resolve_existing(path)
    if not directory.is_dir() or is_blocked(directory):
        raise HTTPException(403, "This directory cannot be browsed")
    try:
        directories = [
            {"name": child.name, "path": str(child.resolve())}
            for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold())
            if child.is_dir() and not is_blocked(child.resolve())
        ]
    except PermissionError as exc:
        raise HTTPException(403, "Permission denied") from exc
    parent = directory.parent if directory != Path("/") else None
    return {"path": str(directory), "parent": str(parent) if parent else None, "directories": directories}


@app.get("/api/roots")
def list_roots() -> list[dict]:
    return [dict(row) for row in roots()]


@app.post("/api/roots", status_code=201)
def add_root(item: RootCreate) -> dict:
    directory = resolve_existing(item.path)
    if not directory.is_dir() or is_blocked(directory):
        raise HTTPException(400, "Choose an accessible media directory")
    name = (item.name or directory.name or str(directory)).strip()
    try:
        with connection() as db:
            cursor = db.execute("INSERT INTO library_roots(kind, path, name) VALUES (?, ?, ?)", (item.kind, str(directory), name))
            root_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "That directory is already in this library") from exc
    return {"id": root_id, "kind": item.kind, "path": str(directory), "name": name}


@app.delete("/api/roots/{root_id}")
def remove_root(root_id: int) -> dict[str, bool]:
    with connection() as db:
        cursor = db.execute("DELETE FROM library_roots WHERE id = ?", (root_id,))
        if not cursor.rowcount:
            raise HTTPException(404, "Library root not found")
    return {"deleted": True}


@app.get("/api/movies")
def movies() -> list[dict]:
    files = media_files("movies")
    return sorted(files, key=lambda item: (item["root_name"].casefold(), item["relative_path"].casefold()))


@app.get("/api/tv")
def tv_shows() -> list[dict]:
    grouped: dict[tuple[int, str], dict] = {}
    for item in media_files("tv"):
        parts = item.pop("parts")
        show_name = parts[0] if len(parts) > 1 else item["root_name"]
        season_name = parts[1] if len(parts) > 2 else "Unsorted"
        key = (item["root_id"], show_name)
        show = grouped.setdefault(key, {"id": f'{item["root_id"]}:{show_name}', "name": show_name, "root_name": item["root_name"], "seasons": {}})
        show["seasons"].setdefault(season_name, []).append(item)
    result = []
    for show in grouped.values():
        seasons = [
            {"name": name, "episodes": sorted(episodes, key=lambda episode: episode["relative_path"].casefold())}
            for name, episodes in sorted(show.pop("seasons").items(), key=lambda pair: season_sort(pair[0]))
        ]
        show["seasons"] = seasons
        show["episode_count"] = sum(len(season["episodes"]) for season in seasons)
        result.append(show)
    return sorted(result, key=lambda show: (show["name"].casefold(), show["root_name"].casefold()))


@app.post("/api/media/batch-probe")
def batch_probe(request: BatchProbeRequest) -> dict:
    paths = [authorized_file(value) for value in dict.fromkeys(request.paths)]
    slots: dict[tuple[str, int], dict] = {}
    failures = []
    for path in paths:
        try:
            counters = {"video": 0, "audio": 0, "subtitle": 0}
            for stream in probe(path).get("streams", []):
                codec_type = stream.get("codec_type")
                if codec_type not in counters:
                    continue
                type_index = counters[codec_type]
                counters[codec_type] += 1
                key = (codec_type, type_index)
                slot = slots.setdefault(key, {"codec_type": codec_type, "type_index": type_index, "present_count": 0, "codecs": set(), "languages": set(), "regions": set(), "titles": set()})
                slot["present_count"] += 1
                slot["codecs"].add(stream.get("codec_name") or "unknown")
                tags = stream.get("tags") or {}
                language, region = split_language(tags.get("language") or "")
                slot["languages"].add(language)
                slot["regions"].add(region)
                slot["titles"].add(tags.get("title") or "")
        except HTTPException as exc:
            failures.append({"path": str(path), "error": exc.detail})
    order = {"video": 0, "audio": 1, "subtitle": 2}
    serialized = []
    for slot in sorted(slots.values(), key=lambda value: (order[value["codec_type"]], value["type_index"])):
        for field in ("codecs", "languages", "regions", "titles"):
            slot[field] = sorted(slot[field], key=str.casefold)
        serialized.append(slot)
    return {"file_count": len(paths), "streams": serialized, "failures": failures}


def make_language(language: str | None, region: str | None) -> str:
    language = (language or "").strip().lower()
    region = (region or "").strip().upper()
    if region and not language:
        raise HTTPException(400, "A region requires a language")
    if language and not re.match(r"^[A-Za-z]{2,3}$", language):
        raise HTTPException(400, f"Invalid language code: {language}")
    if region and not re.match(r"^(?:[A-Za-z]{2}|\d{3})$", region):
        raise HTTPException(400, f"Invalid region code: {region}")
    return f"{language}-{region}" if region else language


@app.post("/api/media/edit")
def edit_media(request: EditRequest) -> dict:
    edited, skipped, failures = [], [], []
    for value in dict.fromkeys(request.paths):
        source = authorized_file(value)
        original = source.stat()
        streams = probe(source).get("streams", [])
        counts = {kind: sum(1 for stream in streams if stream.get("codec_type") == kind) for kind in TYPE_SPECIFIER}
        applicable = [update for update in request.streams if update.type_index < counts[update.codec_type]]
        if not applicable:
            skipped.append(str(source))
            continue
        temporary = source.with_name(f".{source.stem}.{uuid.uuid4().hex}.vse{source.suffix}")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0", "-map_metadata", "0", "-c", "copy"]
        for update in applicable:
            selector = f'{TYPE_SPECIFIER[update.codec_type]}:{update.type_index}'
            if update.language is not None or update.region is not None:
                command += [f"-metadata:s:{selector}", f"language={make_language(update.language, update.region)}"]
            if update.title is not None:
                command += [f"-metadata:s:{selector}", f"title={update.title.strip()}"]
        command.append(str(temporary))
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
            os.chmod(temporary, original.st_mode)
            os.utime(temporary, ns=(original.st_atime_ns, original.st_mtime_ns))
            os.replace(temporary, source)
            edited.append(str(source))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            temporary.unlink(missing_ok=True)
            failures.append({"path": str(source), "error": (getattr(exc, "stderr", None) or "Edit failed")[-1500:]})
    return {"edited": edited, "skipped": skipped, "failures": failures}
