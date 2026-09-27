"""Small DB-API compatibility layer used during the PostgreSQL cut-over.

The application historically used SQLite's lightweight API directly. This
adapter keeps the existing query call sites working while the schema is
rebuilt in PostgreSQL; it is deliberately limited to the SQL idioms used by
VideoStreamEdit and is not a general SQLite emulation layer.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class Row(dict):
    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _replace_qmarks(sql: str) -> str:
    sentinel = "__VSE_PARAMETER__"
    text = sql.replace("?", sentinel).replace("%", "%%")
    return text.replace(sentinel, "%s")


def _translate_sql(sql: str) -> str:
    text = _replace_qmarks(sql.strip())
    text = re.sub(r'=([\s]*)"([^"\n]+)"', r"=\1'\2'", text)
    text = re.sub(r"\bdatetime\s*\(\s*'now'\s*\)", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCURRENT_TIMESTAMP\b", "CURRENT_TIMESTAMP", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCOLLATE\s+NOCASE\b", "COLLATE \"C\"", text, flags=re.IGNORECASE)
    text = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", text, flags=re.IGNORECASE)
    text = re.sub(r"\bINT\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", text, flags=re.IGNORECASE)
    replace = re.match(r"INSERT\s+OR\s+REPLACE\s+INTO\s+([\w\"]+)\s*\(([^)]+)\)\s*(VALUES|SELECT)\s*", text, re.IGNORECASE | re.DOTALL)
    ignore = bool(re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", text, re.IGNORECASE))
    if replace:
        _table, columns, _keyword = replace.groups()
        names = [column.strip() for column in columns.split(",")]
        updates = ", ".join(f"{name}=EXCLUDED.{name}" for name in names if name.strip('\"') != "id")
        text = re.sub(r"^INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", text, count=1, flags=re.IGNORECASE)
        text += f" ON CONFLICT DO UPDATE SET {updates}" if updates else " ON CONFLICT DO NOTHING"
    elif ignore:
        text = re.sub(r"^INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", text, count=1, flags=re.IGNORECASE)
        text += " ON CONFLICT DO NOTHING"
    return text


class Cursor:
    def __init__(self, owner: Connection, cursor: psycopg.Cursor, statement: str = ""):
        self.owner = owner
        self.cursor = cursor
        self.statement = statement
        self.rowcount = cursor.rowcount
        self.lastrowid = None

    def _rows(self, rows: Iterable[dict] | None) -> list[Row] | None:
        if rows is None:
            return None
        return [Row(row) for row in rows]

    def fetchone(self) -> Row | None:
        row = self.cursor.fetchone()
        return Row(row) if row is not None else None

    def fetchall(self) -> list[Row]:
        return [Row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection(AbstractContextManager):
    def __init__(self, url: str | None = None):
        if not url:
            raise RuntimeError("DATABASE_URL is required for PostgreSQL mode")
        self.raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)
        self.total_changes = 0

    def execute(self, sql: str, params: tuple | list = ()) -> Cursor:
        translated = _translate_sql(sql)
        if "ON CONFLICT DO UPDATE SET" in translated:
            table_match = re.search(r"INSERT\s+INTO\s+([\w\"]+)", translated, re.IGNORECASE)
            table_name = table_match.group(1).strip('"') if table_match else ""
            pk_rows = self.raw.execute("""
                SELECT a.attname AS name
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
                 WHERE i.indrelid=%s::regclass AND i.indisprimary
                 ORDER BY array_position(i.indkey, a.attnum)
            """, (table_name,)).fetchall() if table_name else []
            if pk_rows:
                target = ", ".join('"' + row["name"].replace('"', '""') + '"' for row in pk_rows)
                translated = translated.replace("ON CONFLICT DO UPDATE SET", f"ON CONFLICT ({target}) DO UPDATE SET", 1)
            else:
                translated = translated.replace("ON CONFLICT DO UPDATE SET", "ON CONFLICT DO NOTHING", 1)
        savepoint = "vse_stmt_guard"
        # Only schema alterations need statement-level recovery for a caught
        # DuplicateColumn. Normal statements already belong to the connection
        # transaction; wrapping each read/write adds two network round trips
        # without recovering any of their errors.
        guard_schema = bool(re.match(r"\s*ALTER\s+TABLE\b", translated, re.IGNORECASE))
        generated_id = False
        if re.match(r"\s*INSERT\s+INTO\b", translated, re.IGNORECASE) and not re.search(r"\bRETURNING\b", translated, re.IGNORECASE):
            table = re.search(r"INSERT\s+INTO\s+([\w\"]+)", translated, re.IGNORECASE)
            table_name = table.group(1).strip('"') if table else ""
            # RETURNING binds the ID to this statement, unlike a sequence's
            # global last_value, which another worker can advance concurrently.
            has_id = self.raw.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name='id'", (table_name,)).fetchone()
            if has_id:
                translated = translated.rstrip(';') + ' RETURNING id'
                generated_id = True
        if guard_schema:
            self.raw.execute(f"SAVEPOINT {savepoint}")
        try:
            cur = self.raw.execute(translated, params or ())
            if guard_schema:
                self.raw.execute(f"RELEASE SAVEPOINT {savepoint}")
        except psycopg.errors.DuplicateColumn as exc:
            if not guard_schema:
                raise
            self.raw.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.raw.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self.total_changes += max(cur.rowcount, 0)
        wrapper = Cursor(self, cur, translated)
        if generated_id:
            values = cur.fetchall()
            wrapper.lastrowid = values[-1]['id'] if values else None
        return wrapper

    def executemany(self, sql: str, seq: Iterable[tuple]) -> Cursor:
        cur = self.raw.cursor()
        translated = _translate_sql(sql)
        if "ON CONFLICT DO UPDATE SET" in translated:
            table_match = re.search(r'INSERT\s+INTO\s+([\w"]+)', translated, re.IGNORECASE)
            table_name = table_match.group(1).strip('"') if table_match else ""
            pk_rows = self.raw.execute("""
                SELECT a.attname AS name
                  FROM pg_index i
                  JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
                 WHERE i.indrelid=%s::regclass AND i.indisprimary
                 ORDER BY array_position(i.indkey, a.attnum)
            """, (table_name,)).fetchall() if table_name else []
            if pk_rows:
                target = ", ".join('"' + row["name"].replace('"', '""') + '"' for row in pk_rows)
                translated = translated.replace("ON CONFLICT DO UPDATE SET", f"ON CONFLICT ({target}) DO UPDATE SET", 1)
            else:
                translated = translated.replace("ON CONFLICT DO UPDATE SET", "ON CONFLICT DO NOTHING", 1)
        cur.executemany(translated, seq)
        self.total_changes += max(cur.rowcount, 0)
        return Cursor(self, cur, translated)

    def executescript(self, script: str) -> None:
        statements = [part.strip() for part in script.split(";") if part.strip()]
        for statement in statements:
            try:
                self.execute(statement)
            except psycopg.errors.DuplicateColumn:
                continue

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.rollback()
        else:
            self.commit()
        self.close()


def connect() -> Connection:
    return Connection(DATABASE_URL)
