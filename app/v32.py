from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

import app.v28 as v28_module
from app.v2 import is_blocked, resolve_existing
from app.v11 import connection
from app.v31 import app


class OutputFolderRequest(BaseModel):
    path: str


@app.on_event("startup")
def initialize_output_folder() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS import_output_config (
                id INTEGER PRIMARY KEY CHECK(id=1), root_folder TEXT NOT NULL DEFAULT '', last_folder TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS import_output_folders (
                path TEXT PRIMARY KEY
            );
        """)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(import_output_config)")}
        if "last_folder" not in columns:
            db.execute("ALTER TABLE import_output_config ADD COLUMN last_folder TEXT NOT NULL DEFAULT ''")


def output_root() -> Path | None:
    with connection() as db:
        row = db.execute("SELECT root_folder FROM import_output_config WHERE id=1").fetchone()
    if not row or not row["root_folder"]:
        return None
    root = Path(row["root_folder"]).resolve()
    return root if root.is_dir() and not is_blocked(root) else None


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def configured_output_destinations() -> list[dict]:
    root = output_root()
    if not root:
        return []
    with connection() as db:
        saved = [Path(row["path"]).resolve() for row in db.execute("SELECT path FROM import_output_folders ORDER BY path COLLATE NOCASE")]
    folders = [root, *(path for path in saved if path != root and path.is_dir() and inside(path, root))]
    return [{"path": str(path), "name": "Default output folder" if path == root else f"Selected · {path.name}", "movie_count": 0} for path in folders]


v28_module.movie_destinations = configured_output_destinations


@app.get("/api/v32/import/output/config")
def get_output_config() -> dict:
    root = output_root()
    with connection() as db:
        row = db.execute("SELECT last_folder FROM import_output_config WHERE id=1").fetchone()
    last = Path(row["last_folder"]).resolve() if row and row["last_folder"] else None
    if not root or not last or not last.is_dir() or not inside(last, root) or is_blocked(last):
        last = root
    return {"output_folder": str(root) if root else "", "last_output_folder": str(last) if last else ""}


@app.put("/api/v32/import/output/config")
def save_output_config(request: OutputFolderRequest) -> dict:
    root = resolve_existing(request.path)
    if not root.is_dir() or is_blocked(root):
        raise HTTPException(403, "This folder cannot be used as the movie output root")
    with connection() as db:
        db.execute("INSERT INTO import_output_config(id,root_folder,last_folder) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET root_folder=excluded.root_folder,last_folder=excluded.last_folder", (str(root), str(root)))
        db.execute("DELETE FROM import_output_folders")
    return {"output_folder": str(root)}


@app.get("/api/v32/import/output/browse")
def browse_output_folder(path: str | None = None) -> dict:
    root = output_root()
    if not root:
        raise HTTPException(400, "Configure the movie output folder in Setup first")
    current = resolve_existing(path) if path else root
    if not current.is_dir() or not inside(current, root) or is_blocked(current):
        raise HTTPException(403, "Folder is outside the configured movie output root")
    try:
        directories = [{"name": item.name, "path": str(item.resolve())} for item in sorted(current.iterdir(), key=lambda item: item.name.casefold()) if item.is_dir() and inside(item.resolve(), root) and not is_blocked(item.resolve())]
    except PermissionError as exc:
        raise HTTPException(403, "Permission denied") from exc
    return {"root": str(root), "path": str(current), "directories": directories}


@app.post("/api/v32/import/output/select")
def select_output_folder(request: OutputFolderRequest) -> dict:
    root = output_root()
    if not root:
        raise HTTPException(400, "Configure the movie output folder in Setup first")
    path = resolve_existing(request.path)
    if not path.is_dir() or not inside(path, root) or is_blocked(path):
        raise HTTPException(403, "Folder is outside the configured movie output root")
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO import_output_folders(path) VALUES(?)", (str(path),))
        db.execute("UPDATE import_output_config SET last_folder=? WHERE id=1", (str(path),))
    return {"path": str(path)}
