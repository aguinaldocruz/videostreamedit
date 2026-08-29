from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

import app.v28 as v28_module
import app.v30 as v30_module
from app.v2 import is_blocked, resolve_existing
from app.v11 import connection
from app.v30 import app


base_movie_destinations = v30_module.all_movie_destinations


class CustomDestinationRequest(BaseModel):
    path: str


@app.on_event("startup")
def initialize_custom_destinations() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS import_custom_destinations (path TEXT PRIMARY KEY)")


def plex_movie_roots() -> list[Path]:
    paths = {Path(item["path"]).resolve() for item in base_movie_destinations()}
    return sorted((path for path in paths if not any(other != path and other in path.parents for other in paths)), key=lambda path: str(path))


def inside_plex_movie_tree(path: Path) -> bool:
    return any(path == root or root in path.parents for root in plex_movie_roots())


def destinations_with_custom() -> list[dict]:
    destinations = base_movie_destinations()
    known = {item["path"] for item in destinations}
    with connection() as db:
        custom = [row["path"] for row in db.execute("SELECT path FROM import_custom_destinations")]
    for value in custom:
        path = Path(value)
        if value not in known and path.is_dir() and inside_plex_movie_tree(path.resolve()):
            destinations.append({"path": value, "name": f"Custom · {path.name or value}", "movie_count": 0})
    return destinations


v28_module.movie_destinations = destinations_with_custom
v30_module.all_movie_destinations = destinations_with_custom


@app.get("/api/v31/import/destination/browse")
def browse_destination_folder(path: str) -> dict:
    current = resolve_existing(path)
    if not current.is_dir() or is_blocked(current) or not inside_plex_movie_tree(current):
        raise HTTPException(403, "Folder is outside synchronized Plex movie-library trees")
    try:
        directories = [
            {"name": item.name, "path": str(item.resolve())}
            for item in sorted(current.iterdir(), key=lambda item: item.name.casefold())
            if item.is_dir() and not is_blocked(item.resolve()) and inside_plex_movie_tree(item.resolve())
        ]
    except PermissionError as exc:
        raise HTTPException(403, "Permission denied") from exc
    return {"path": str(current), "directories": directories}


@app.post("/api/v31/import/destinations/custom")
def add_custom_destination(request: CustomDestinationRequest) -> dict:
    path = resolve_existing(request.path)
    if not path.is_dir() or not inside_plex_movie_tree(path):
        raise HTTPException(403, "Destination must remain inside a synchronized Plex movie-library tree")
    with connection() as db:
        db.execute("INSERT OR IGNORE INTO import_custom_destinations(path) VALUES(?)", (str(path),))
    return {"path": str(path), "name": f"Custom · {path.name or path}", "movie_count": 0}
