from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

import app.v28 as v28_module
from app.v11 import connection
from app.v28 import app


class DestinationOrderRequest(BaseModel):
    paths: list[str] = Field(max_length=10000)


@app.on_event("startup")
def initialize_destination_order() -> None:
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS import_destination_order (path TEXT PRIMARY KEY, position INTEGER NOT NULL)")


def all_movie_destinations() -> list[dict]:
    with connection() as db:
        rows = [dict(row) for row in db.execute("SELECT library_key,library_name,path FROM plex_media WHERE kind='movie'")]
        positions = {row["path"]: row["position"] for row in db.execute("SELECT path,position FROM import_destination_order")}
    libraries: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        libraries[row["library_key"]].append(row)
    folders: dict[str, dict] = {}
    for library_rows in libraries.values():
        movie_paths = [Path(row["path"]).resolve() for row in library_rows]
        if not movie_paths:
            continue
        try:
            root = Path(os.path.commonpath([str(path) for path in movie_paths])).resolve()
        except (ValueError, OSError):
            continue
        if root in movie_paths:
            root = root.parent
        library_name = library_rows[0]["library_name"]
        counts: dict[Path, int] = defaultdict(int)
        for movie in movie_paths:
            folder = movie.parent
            while True:
                counts[folder] += 1
                if folder == root or root not in folder.parents:
                    break
                folder = folder.parent
        for folder, count in counts.items():
            if not folder.is_dir():
                continue
            relative = "." if folder == root else str(folder.relative_to(root))
            name = library_name if relative == "." else f"{library_name} · {relative}"
            key = str(folder)
            previous = folders.get(key)
            if not previous or count > previous["movie_count"]:
                folders[key] = {"path": key, "name": name, "movie_count": count}
    return sorted(folders.values(), key=lambda item: (0, positions[item["path"]]) if item["path"] in positions else (1, -item["movie_count"], item["name"].casefold(), item["path"]))


v28_module.movie_destinations = all_movie_destinations


@app.put("/api/v30/import/destinations/order")
def save_destination_order(request: DestinationOrderRequest) -> dict:
    available = {item["path"] for item in all_movie_destinations()}
    ordered = []
    seen = set()
    for path in request.paths:
        if path in available and path not in seen:
            ordered.append(path)
            seen.add(path)
    ordered.extend(path for path in available if path not in seen)
    with connection() as db:
        db.execute("DELETE FROM import_destination_order")
        db.executemany("INSERT INTO import_destination_order(path,position) VALUES(?,?)", [(path, index) for index, path in enumerate(ordered)])
    return {"saved": len(ordered)}
