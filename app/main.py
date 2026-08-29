from __future__ import annotations

import json
import os
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
BLOCKED_BROWSE_PATHS = {Path("/proc"), Path("/sys"), Path("/dev"), CONFIG_DIR.resolve()}

app = FastAPI(title="VideoStreamEdit", version="0.1.0")


class RootCreate(BaseModel):
    kind: Literal["movies", "tv"]
    path: str = Field(min_length=1)
    name: str | None = None


class StreamUpdate(BaseModel):
    stream_index: int = Field(ge=0)
    language: str | None = None
    region: str | None = None
    title: str | None = None
    default: bool | None = None
    forced: bool | None = None


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


def clean_path(value: str) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(400, "Directory does not exist or cannot be accessed") from exc


def is_blocked(path: Path) -> bool:
    return any(path == blocked or blocked in path.parents for blocked in BLOCKED_BROWSE_PATHS)


def configured_roots() -> list[sqlite3.Row]:
    with connection() as db:
        return db.execute("SELECT id, kind, path, name FROM library_roots ORDER BY kind, name").fetchall()


def authorized_media_path(value: str) -> Path:
    path = clean_path(value)
    if not path.is_file():
        raise HTTPException(400, "Media path is not a file")
    for root in configured_roots():
        root_path = Path(root["path"])
        if path == root_path or root_path in path.parents:
            return path
    raise HTTPException(403, "Media path is outside configured library roots")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/app.css")
def stylesheet() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.css", media_type="text/css")


@app.get("/app.js")
def javascript() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/browse")
def browse(path: str = Query(default="/")) -> dict:
    directory = clean_path(path)
    if not directory.is_dir() or is_blocked(directory):
        raise HTTPException(403, "This directory cannot be browsed")
    children = []
    try:
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            if child.is_dir() and not is_blocked(child.resolve()):
                children.append({"name": child.name, "path": str(child.resolve())})
    except PermissionError as exc:
        raise HTTPException(403, "Permission denied") from exc
    parent = directory.parent if directory != Path("/") else None
    return {"path": str(directory), "parent": str(parent) if parent else None, "directories": children}


@app.get("/api/roots")
def list_roots() -> list[dict]:
    return [dict(row) for row in configured_roots()]


@app.post("/api/roots", status_code=201)
def add_root(item: RootCreate) -> dict:
    directory = clean_path(item.path)
    if not directory.is_dir() or is_blocked(directory):
        raise HTTPException(400, "Choose an accessible media directory")
    name = (item.name or directory.name or str(directory)).strip()
    try:
        with connection() as db:
            cursor = db.execute(
                "INSERT INTO library_roots(kind, path, name) VALUES (?, ?, ?)",
                (item.kind, str(directory), name),
            )
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


@app.get("/api/library")
def library(kind: Literal["movies", "tv"] | None = None) -> list[dict]:
    results: list[dict] = []
    for root in configured_roots():
        if kind and root["kind"] != kind:
            continue
        root_path = Path(root["path"])
        if not root_path.is_dir():
            continue
        try:
            files = root_path.rglob("*")
            for path in files:
                if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                    stat = path.stat()
                    results.append({
                        "path": str(path), "name": path.name,
                        "relative_path": str(path.relative_to(root_path)),
                        "root_id": root["id"], "root_name": root["name"], "kind": root["kind"],
                        "size": stat.st_size, "modified": int(stat.st_mtime),
                    })
        except PermissionError:
            continue
    return sorted(results, key=lambda item: (item["kind"], item["root_name"].casefold(), item["relative_path"].casefold()))


def probe(path: Path) -> dict:
    command = ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
        return json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise HTTPException(503, "ffprobe is not installed") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", None) or "Could not inspect media file"
        raise HTTPException(422, detail[-1000:]) from exc


@app.get("/api/media/probe")
def inspect_media(path: str) -> dict:
    media = authorized_media_path(path)
    data = probe(media)
    return {"path": str(media), **data}


def language_tag(language: str | None, region: str | None) -> str | None:
    language = language.strip() if language else ""
    region = region.strip().upper() if region else ""
    if region and not language:
        raise HTTPException(400, "A region requires a language")
    return f"{language}-{region}" if region else language or None


@app.post("/api/media/edit")
def edit_media(request: EditRequest) -> dict:
    edited = []
    for value in request.paths:
        source = authorized_media_path(value)
        original_stat = source.stat()
        temporary = source.with_name(f".{source.stem}.{uuid.uuid4().hex}.vse{source.suffix}")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(source), "-map", "0", "-map_metadata", "0", "-c", "copy"]
        for update in request.streams:
            tag = language_tag(update.language, update.region)
            if tag is not None:
                command += [f"-metadata:s:{update.stream_index}", f"language={tag}"]
            if update.title is not None:
                command += [f"-metadata:s:{update.stream_index}", f"title={update.title.strip()}"]
            if update.default is not None or update.forced is not None:
                flags = []
                if update.default:
                    flags.append("default")
                if update.forced:
                    flags.append("forced")
                command += [f"-disposition:{update.stream_index}", "+".join(flags) or "0"]
        command += [str(temporary)]
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=3600, check=True)
            os.chmod(temporary, original_stat.st_mode)
            os.utime(temporary, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            os.replace(temporary, source)
            edited.append(str(source))
        except FileNotFoundError as exc:
            raise HTTPException(503, "ffmpeg is not installed") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            temporary.unlink(missing_ok=True)
            detail = getattr(exc, "stderr", None) or "Media edit failed"
            raise HTTPException(422, detail[-2000:]) from exc
    return {"edited": edited}
