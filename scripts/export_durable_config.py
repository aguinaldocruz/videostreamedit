#!/usr/bin/env python3
"""Export user-owned configuration from the legacy SQLite database.

Historical jobs and derived indexes are deliberately excluded. The resulting
JSON snapshot is an input to the PostgreSQL cut-over and is safe to rerun.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


TABLES = (
    "plex_config",
    "plex_libraries",
    "library_roots",
    "import_config",
    "import_destination_order",
    "import_custom_destinations",
    "import_output_config",
    "import_output_folders",
    "reusable_stream_values",
    "application_settings",
    "track_name_correction_history",
    "media_notes",
    "language_detection_settings",
    "task_queue_settings",
    "index_job_schedule",
    "plex_sync_schedule",
)


def export(source: Path, destination: Path) -> None:
    db = sqlite3.connect(source)
    db.row_factory = sqlite3.Row
    snapshot: dict[str, list[dict]] = {}
    for table in TABLES:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        snapshot[table] = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"exported {sum(len(rows) for rows in snapshot.values())} durable rows to {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    export(args.source, args.destination)

