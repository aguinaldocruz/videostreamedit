from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

import app.v5 as v5_module
import app.v7 as v7_module
import app.v13 as v13_module
from app.v5 import external_subtitles
from app.v7 import ReorderEditRequest
from app.v11 import connection, plex_authorized_file, column_exists
from app.v25 import app
from app import movie_import_progress

app.include_router(movie_import_progress.router)

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
    media_kind: Literal['movie', 'episode'] = 'movie'
    remove_original: bool = False
    html_cleanups: list[ImportHtmlCleanup] = Field(default_factory=list)
    operation_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


class ImportCleanupRequest(BaseModel):
    source: str
    expected_source: dict
    expected_subtitles: list[dict]
    target: str
    expected_target: dict
    expected_target_subtitles: list[dict]


@app.on_event("startup")
def initialize_movie_import() -> None:
    from app.movie_import_pipeline import recover_interrupted_imports
    recover_interrupted_imports()
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS import_config (id INTEGER PRIMARY KEY CHECK(id=1), input_folder TEXT NOT NULL DEFAULT '')")
        if not column_exists(db, 'import_config', 'last_input_folder'):
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
        raise HTTPException(400, "Configure the media input folder in Setup first")
    current = Path(path).resolve() if path else root
    if not current.is_dir() or not inside(current, root):
        raise HTTPException(403, "Folder is outside the configured media input folder")
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


def queue_post_import_refresh(source: Path, target: Path, media_kind: str = 'movie') -> None:
    """Ask Plex to discover the copy before VideoStreamEdit indexes it."""
    from app.v65 import enqueue

    with connection() as db:
        libraries = [dict(row) for row in db.execute(
            "SELECT library_key,path FROM plex_media WHERE kind=?", (media_kind,)
        )]
    # Select the destination library, never the source's old Plex identity.
    grouped: dict[str, list[str]] = {}
    for row in libraries:
        grouped.setdefault(str(row["library_key"]), []).append(row["path"])
    candidates: list[tuple[int, str]] = []
    for key, paths in grouped.items():
        try:
            root = Path(os.path.commonpath(paths)).resolve()
        except (ValueError, OSError):
            continue
        if str(root) in paths:
            root = root.parent
        if inside(target, root):
            candidates.append((len(root.parts), key))
    library_key = max(candidates)[1] if candidates else ''
    if not library_key:
        raise RuntimeError('Destination Plex library could not be identified; run Plex sync to discover the import')
    enqueue(
        "plex_import_refresh",
        {"path": str(target), "library_key": library_key, "rating_key": ""},
        f"Discover and index {target.name}",
        deduplicate=True,
    )


@app.post("/api/v28/import/movie")
def import_movie(request: MovieImportRequest) -> dict:
    operation_id = request.operation_id
    if operation_id:
        movie_import_progress.start(operation_id, total=7 if request.remove_original else 6)
    try:
        result = execute_movie_import(request, lambda step, message, detail='', **kw:
                                      movie_import_progress.update(operation_id, step, message, detail, **kw)
                                      if operation_id else None)
    except Exception as exc:
        if operation_id:
            movie_import_progress.finish(operation_id, str(getattr(exc, 'detail', exc)))
        raise
    if operation_id:
        movie_import_progress.finish(operation_id)
    return result


def execute_movie_import(request: MovieImportRequest, progress=None) -> dict:
    from app.job_safety import stamp
    from app.movie_import_pipeline import build_import, publish_import_cache
    progress = progress or (lambda *args, **kw: None)
    progress(1, 'Validating media import', 'Checking source, destination and available disk space')
    source = authorized_import_file(request.source)
    if request.remove_original:
        root = import_input_root()
        if not root or not inside(source, root):
            raise HTTPException(403, 'Original removal requires a source inside the configured input folder')
    destinations = {item["path"] for item in movie_destinations()}
    destination = Path(request.destination).resolve()
    if not any(destination == Path(value) or Path(value) in destination.parents for value in destinations):
        raise HTTPException(403, "Choose a folder inside the configured media output root")
    filename = Path(request.filename).name
    if not filename or filename != request.filename or Path(filename).suffix.lower() != source.suffix.lower():
        raise HTTPException(400, f"Output filename must use the original {source.suffix} extension and cannot contain folders")
    target = destination / filename
    if target.exists():
        raise HTTPException(409, "A file with that output name already exists")
    subtitles = external_subtitles(source)
    original = stamp(source)
    original_subtitles = [stamp(Path(item['path'])) for item in subtitles]
    # Resolve all identities against the source once. Never call the ordinary
    # editor on successive movie copies: import has its own one-output commit.
    edit = request.edit.model_copy(update={'path': str(source)})
    result = build_import(source, target, edit, subtitles, request.html_cleanups,
                          progress, original, original_subtitles)
    logger.info("change=movie_imported source=%s target=%s html_cleanups=%d", str(source).replace("\n", "\\n"), str(target).replace("\n", "\\n"), len(request.html_cleanups))
    warnings = list(result.get('warnings', []))
    try:
        progress(6, 'Finishing media import', 'Saving subtitle cache and import preferences')
    except Exception:
        warnings.append('Media imported; final progress update could not be recorded.')
    for reason, callback in (
        ('Cleaned subtitle cache could not be saved; it can be collected later.',
         lambda: publish_import_cache(target, result['plan'])),
        ('Track-name learning could not be recorded.',
         lambda: _record_import_learning(edit, result['plan'])),
        ('Final-revision preference could not be saved; set it after Plex sync.',
         lambda: _record_import_final_revision(edit, target, request.media_kind)),
        ('Approved audio was imported, but its staging records still need cleanup.',
         lambda: _complete_import_audio(edit)),
    ):
        try:
            callback()
        except Exception as exc:
            warnings.append(reason)
            logger.warning('movie_import event=post_commit_update_failed target=%s error=%s', target, str(exc).replace('\n', ' ')[:300])
    removed = []
    cleanup_status = 'not_requested'
    if request.remove_original:
        cleanup_status = 'retained'
        try:
            # Keep full-precision timestamps on the server. JSON -> JavaScript
            # -> JSON rounds nanosecond integers and used to reject unchanged
            # originals after a successful immediate import.
            progress(7, 'Removing originals', 'Import verified; checking source identities before removal')
            if not result.get('durable'):
                raise RuntimeError('Output durability was not confirmed; originals retained for review')
            removed = cleanup_import_source(ImportCleanupRequest(
                source=str(source), expected_source=original, expected_subtitles=original_subtitles,
                target=str(target), expected_target=result['target_snapshot'],
                expected_target_subtitles=result['target_subtitles'],
            ))['removed']
            cleanup_status = 'removed'
        except Exception as exc:
            warning = str(getattr(exc, 'detail', exc))
            warnings.append(f'Import succeeded, but source cleanup needs attention: {warning}')
            logger.warning('movie_import event=original_cleanup_failed source=%s error=%s',
                           str(source).replace('\n', '\\n'), warning.replace('\n', ' ')[:500])
    try:
        queue_post_import_refresh(source, target, request.media_kind)
    except Exception as exc:
        # A refresh failure cannot make a successfully imported file look like
        # a failed/retryable copy or cause the source to be lost.
        warnings.append('Media imported; Plex refresh could not be queued. Run Plex sync to discover it.')
        logger.warning('plex_sync event=post_import_queue_failed target=%s error=%s', target, str(exc).replace('\n', ' ')[:400])
    return {"source": str(source), "target": str(target), "external_subtitles": result['external_subtitles'],
            "source_snapshot": original, "source_subtitles": original_subtitles, "warnings": warnings,
            "media_kind": request.media_kind, "originals_removed": removed, "source_cleanup_status": cleanup_status,
            "operation": result['operation'], "media_writes": result['media_writes']}


def _record_import_learning(edit: ReorderEditRequest, plan: dict) -> None:
    from app.v40 import record_track_name_corrections
    from app.v43 import old_track_names, typed_streams
    record_track_name_corrections(edit, old_track_names(typed_streams(plan['data'])))


def _record_import_final_revision(edit: ReorderEditRequest, target: Path, media_kind: str = 'movie') -> None:
    if edit.final_version is None:
        return
    # The imported copy has not yet been discovered by Plex. Keep this explicit
    # user choice on the imported media, never on the source's catalog entry.
    entity_type = 'tv' if media_kind == 'episode' else 'movie'
    entity_key = 'episode:' + str(target) if media_kind == 'episode' else str(target)
    with connection() as db:
        db.execute("""INSERT INTO media_notes(entity_type,entity_key,note,reviewed,plex_sync_change,final_version,updated_at)
                   VALUES(?,?,'',?,0,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(entity_type,entity_key) DO UPDATE SET
                   final_version=excluded.final_version,reviewed=CASE WHEN excluded.final_version=1 THEN 1 ELSE media_notes.reviewed END,
                   updated_at=CURRENT_TIMESTAMP""", (entity_type, entity_key, int(edit.final_version), int(edit.final_version)))


def _complete_import_audio(edit: ReorderEditRequest) -> None:
    if edit.audio_compatibility:
        from app.review_audio import integration_complete
        integration_complete(edit.audio_compatibility)


def cleanup_import_source(request: ImportCleanupRequest) -> dict:
    """Internal post-commit cleanup: fingerprints never round-trip through JS."""
    from app.job_safety import stamp
    source = authorized_import_file(request.source)
    root = import_input_root()
    if not root or not inside(source, root):
        raise HTTPException(403, "Only files in the configured input folder can be removed")
    target = Path(request.target).resolve()
    if (target == source or not target.is_file() or target.samefile(source)
            or stamp(target) != request.expected_target):
        raise HTTPException(409, 'Imported media is missing or changed; originals were retained')
    for saved in request.expected_target_subtitles:
        if stamp(Path(saved['path'])) != saved:
            raise HTTPException(409, 'Imported subtitle changed; originals were retained')
    subtitles = [Path(item["path"]) for item in external_subtitles(source)]
    if any(path.parent != source.parent or not inside(path, root) for path in subtitles):
        raise HTTPException(409, 'Source subtitle resolves outside its source folder; originals were retained')
    if stamp(source) != request.expected_source:
        raise HTTPException(409, 'Source media changed after import; originals were retained')
    expected = {item['path']: item for item in request.expected_subtitles}
    if {str(path) for path in subtitles} != set(expected) or any(stamp(path) != expected[str(path)] for path in subtitles):
        raise HTTPException(409, 'Source subtitles changed after import; originals were retained')
    if target in subtitles or any(saved['path'] in expected for saved in request.expected_target_subtitles):
        raise HTTPException(409, 'Import source and destination subtitle paths overlap; originals were retained')
    removed = []
    for subtitle in subtitles:
        if stamp(subtitle) != expected[str(subtitle)]:
            raise HTTPException(409, 'Source subtitle changed before removal; remaining originals retained')
        subtitle.unlink()
        removed.append(str(subtitle))
    if stamp(source) != request.expected_source or stamp(target) != request.expected_target:
        raise HTTPException(409, 'Media changed before source removal; original media retained')
    source.unlink()
    removed.append(str(source))
    logger.info("change=movie_import_originals_removed files=%s", "|".join(value.replace("\n", "\\n") for value in removed))
    return {"removed": removed}
