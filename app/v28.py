from __future__ import annotations

import logging
import os
import shutil
from collections import Counter
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v7 as v7_module
import app.v13 as v13_module
import app.v5 as v5_module
from app.v5 import external_subtitles
from app.v7 import OrderItem, ReorderEditRequest, reorder_edit
from app.v11 import connection, plex_authorized_file
from app.v25 import app


logger = logging.getLogger("videostreamedit")
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts"}


class ImportConfigRequest(BaseModel):
    input_folder: str


class ImportHtmlCleanup(BaseModel):
    type_index: int | None = None
    external_path: str | None = None


class MovieImportRequest(BaseModel):
    source: str
    destination: str
    filename: str
    edit: ReorderEditRequest
    html_cleanups: list[ImportHtmlCleanup] = Field(default_factory=list)


class ImportCleanupRequest(BaseModel):
    source: str


@app.on_event("startup")
def initialize_movie_import() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS import_config (id INTEGER PRIMARY KEY CHECK(id=1), input_folder TEXT NOT NULL DEFAULT '')")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(import_config)")}
        if "last_input_folder" not in columns:
            db.execute("ALTER TABLE import_config ADD COLUMN last_input_folder TEXT NOT NULL DEFAULT ''")


def import_input_root() -> Path | None:
    with connection() as db:
        row = db.execute("SELECT input_folder FROM import_config WHERE id=1").fetchone()
    if not row or not row["input_folder"]:
        return None
    root = Path(row["input_folder"]).resolve()
    return root if root.is_dir() else None


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def movie_destinations() -> list[dict]:
    with connection() as db:
        rows = [dict(row) for row in db.execute("SELECT library_key,library_name,path FROM plex_media WHERE kind='movie'")]
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["library_key"], []).append(row)
    counts: Counter[tuple[str, str]] = Counter()
    labels = {}
    for library_rows in grouped.values():
        paths = [row["path"] for row in library_rows]
        try:
            root = Path(os.path.commonpath(paths)).resolve()
        except (ValueError, OSError):
            continue
        if root.is_file():
            root = root.parent
        library_name = library_rows[0]["library_name"]
        counts[(str(root), library_name)] += len(paths)
        labels[str(root)] = library_name
        for value in paths:
            try:
                relative = Path(value).resolve().relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) > 1:
                folder = root / relative.parts[0]
                counts[(str(folder), library_name)] += 1
                labels[str(folder)] = f"{library_name} · {relative.parts[0]}"
    found = [{"path": path, "name": labels[path], "movie_count": count} for (path, _), count in counts.items() if Path(path).is_dir()]
    return sorted(found, key=lambda item: (-item["movie_count"], item["name"].casefold(), item["path"]))


def authorized_import_file(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_file():
        raise HTTPException(400, "Media path is not a file")
    root = import_input_root()
    if root and inside(path, root):
        return path
    for destination in movie_destinations():
        if inside(path, Path(destination["path"])):
            return path
    return plex_authorized_file(value)


v7_module.authorized_file = authorized_import_file
v13_module.plex_authorized_file = authorized_import_file
v5_module.authorized_file = authorized_import_file


@app.get("/api/v28/import/config")
def get_import_config() -> dict:
    root = import_input_root()
    with connection() as db:
        row = db.execute("SELECT last_input_folder FROM import_config WHERE id=1").fetchone()
    last = Path(row["last_input_folder"]).resolve() if row and row["last_input_folder"] else None
    if not root or not last or not last.is_dir() or not inside(last, root):
        last = root
    return {"input_folder": str(root) if root else "", "last_input_folder": str(last) if last else ""}


@app.put("/api/v28/import/config")
def save_import_config(request: ImportConfigRequest) -> dict:
    root = Path(request.input_folder).resolve()
    if not root.is_dir():
        raise HTTPException(400, "Input folder does not exist inside the container")
    with connection() as db:
        db.execute("INSERT INTO import_config(id,input_folder,last_input_folder) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET input_folder=excluded.input_folder,last_input_folder=excluded.last_input_folder", (str(root), str(root)))
    logger.info("change=movie_import_input_folder path=%s", str(root).replace("\n", "\\n"))
    return {"input_folder": str(root)}


@app.get("/api/v28/import/browse")
def browse_import_folder(path: str | None = None) -> dict:
    root = import_input_root()
    if not root:
        raise HTTPException(400, "Configure the movie input folder in Setup first")
    current = Path(path).resolve() if path else root
    if not current.is_dir() or not inside(current, root):
        raise HTTPException(403, "Folder is outside the configured movie input folder")
    with connection() as db:
        db.execute("UPDATE import_config SET last_input_folder=? WHERE id=1", (str(current),))
    directories, files = [], []
    try:
        for item in sorted(current.iterdir(), key=lambda value: (not value.is_dir(), value.name.casefold())):
            if item.is_dir():
                directories.append({"name": item.name, "path": str(item.resolve())})
            elif item.is_file() and item.suffix.lower() in MEDIA_EXTENSIONS:
                files.append({"name": item.name, "path": str(item.resolve()), "size": item.stat().st_size})
    except OSError as exc:
        raise HTTPException(400, str(exc)) from exc
    parent = str(current.parent) if current != root and inside(current.parent, root) else None
    return {"root": str(root), "path": str(current), "parent": parent, "directories": directories, "files": files}


@app.get("/api/v28/import/destinations")
def import_destinations() -> list[dict]:
    return movie_destinations()


def target_subtitle_path(source: Path, target: Path, subtitle: Path) -> Path:
    suffix = subtitle.name[len(source.stem):] if subtitle.name.casefold().startswith(source.stem.casefold()) else subtitle.suffix
    return target.with_name(target.stem + suffix)


def queue_post_import_refresh(source: Path, target: Path) -> None:
    """Ask Plex to discover the copy before VideoStreamEdit indexes it."""
    from app.v65 import enqueue

    with connection() as db:
        source_row = db.execute(
            "SELECT library_key,rating_key FROM plex_media WHERE path=?",
            (str(source),),
        ).fetchone()
        libraries = [dict(row) for row in db.execute(
            "SELECT library_key,path FROM plex_media WHERE kind='movie'"
        )]
    library_key = str(source_row["library_key"]) if source_row else ""
    rating_key = str(source_row["rating_key"]) if source_row else ""
    if not library_key:
        grouped: dict[str, list[str]] = {}
        for row in libraries:
            grouped.setdefault(str(row["library_key"]), []).append(row["path"])
        candidates: list[tuple[int, str]] = []
        for key, paths in grouped.items():
            try:
                root = Path(os.path.commonpath(paths)).resolve()
            except (ValueError, OSError):
                continue
            if inside(target, root):
                candidates.append((len(root.parts), key))
        if candidates:
            library_key = max(candidates)[1]
    if not library_key:
        logger.warning("plex_sync event=post_import_not_queued reason=library_not_found target=%s", str(target).replace("\n", "\\n"))
        return
    enqueue(
        "plex_import_refresh",
        {"path": str(target), "library_key": library_key, "rating_key": rating_key},
        f"Discover and index {target.name}",
        deduplicate=True,
    )


@app.post("/api/v28/import/movie")
def import_movie(request: MovieImportRequest) -> dict:
    source = authorized_import_file(request.source)
    destinations = {item["path"] for item in movie_destinations()}
    destination = Path(request.destination).resolve()
    if not any(destination == Path(value) or Path(value) in destination.parents for value in destinations):
        raise HTTPException(403, "Choose a folder inside the configured movie output root")
    filename = Path(request.filename).name
    if not filename or filename != request.filename or Path(filename).suffix.lower() != source.suffix.lower():
        raise HTTPException(400, f"Output filename must use the original {source.suffix} extension and cannot contain folders")
    target = destination / filename
    if target.exists():
        raise HTTPException(409, "A file with that output name already exists")
    subtitles = external_subtitles(source)
    copied_subtitles: dict[str, str] = {}
    try:
        shutil.copy2(source, target)
        for item in subtitles:
            old = Path(item["path"])
            new = target_subtitle_path(source, target, old)
            if new.exists():
                raise HTTPException(409, f"External subtitle already exists: {new.name}")
            shutil.copy2(old, new)
            copied_subtitles[str(old)] = str(new)
        # Cleanup belongs to the imported copy. Doing it before the general edit
        # also keeps subtitle type indexes aligned with the preview the user saw.
        if request.html_cleanups:
            from app.v51 import clean_embedded, clean_external_html
            for cleanup in request.html_cleanups:
                if cleanup.external_path:
                    copied = copied_subtitles.get(cleanup.external_path)
                    if not copied:
                        raise HTTPException(404, "The selected external subtitle was not copied")
                    clean_external_html(Path(copied))
                elif cleanup.type_index is not None:
                    clean_embedded(target, cleanup.type_index)
        payload = request.edit.model_dump()
        payload["path"] = str(target)
        for item in payload["external_subtitles"]:
            item["path"] = copied_subtitles.get(item["path"], item["path"])
        for item in payload["order"]:
            if item.get("path"):
                item["path"] = copied_subtitles.get(item["path"], item["path"])
        for field in ("default_subtitle", "forced_subtitle"):
            value = payload.get(field)
            if value and value.startswith("external:"):
                payload[field] = "external:" + copied_subtitles.get(value[9:], value[9:])
        payload["remove"] = ["external:" + copied_subtitles.get(value[9:], value[9:]) if value.startswith("external:") else value for value in payload["remove"]]
        result = reorder_edit(ReorderEditRequest(**payload))
    except Exception:
        target.unlink(missing_ok=True)
        for value in copied_subtitles.values():
            Path(value).unlink(missing_ok=True)
        raise
    logger.info("change=movie_imported source=%s target=%s html_cleanups=%d", str(source).replace("\n", "\\n"), str(target).replace("\n", "\\n"), len(request.html_cleanups))
    queue_post_import_refresh(source, target)
    return {"source": str(source), "target": str(target), "external_subtitles": list(copied_subtitles.values()), "warnings": result.get("warnings", [])}


@app.post("/api/v28/import/cleanup")
def cleanup_import_source(request: ImportCleanupRequest) -> dict:
    source = authorized_import_file(request.source)
    root = import_input_root()
    if not root or not inside(source, root):
        raise HTTPException(403, "Only files in the configured input folder can be removed")
    subtitles = [Path(item["path"]) for item in external_subtitles(source)]
    source.unlink()
    removed = [str(source)]
    for subtitle in subtitles:
        subtitle.unlink(missing_ok=True)
        removed.append(str(subtitle))
    logger.info("change=movie_import_originals_removed files=%s", "|".join(value.replace("\n", "\\n") for value in removed))
    return {"removed": removed}
