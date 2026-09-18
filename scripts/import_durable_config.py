#!/usr/bin/env python3
"""Import the durable configuration snapshot into the fresh PostgreSQL DB."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def import_snapshot(snapshot_path: Path) -> None:
    url = os.environ["DATABASE_URL"]
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    with psycopg.connect(url, row_factory=dict_row) as db:
        for table, rows in snapshot.items():
            if not rows:
                continue
            columns = {
                row["column_name"]
                for row in db.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s",
                    (table,),
                ).fetchall()
            }
            if not columns:
                continue
            selected = [column for column in rows[0] if column in columns]
            if not selected:
                continue
            db.execute(f'DELETE FROM "{table}"')
            names = ", ".join(f'"{column}"' for column in selected)
            placeholders = ", ".join("%s" for _ in selected)
            for row in rows:
                db.execute(
                    f'INSERT INTO "{table}" ({names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING',
                    tuple(row.get(column) for column in selected),
                )
            print(f"imported {len(rows)} rows into {table}")


if __name__ == "__main__":
    import_snapshot(Path(os.environ.get("SNAPSHOT", "/data/migration/durable-config.json")))

