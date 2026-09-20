"""Small DB-API compatibility layer used during the PostgreSQL cut-over.

The application historically used SQLite's lightweight API directly. This
adapter keeps the existing query call sites working while the schema is
rebuilt in PostgreSQL; it is deliberately limited to the SQL idioms used by
VideoStreamEdit and is not a general SQLite emulation layer.
"""

from __future__ import annotations

import os
import re
import sqlite3
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
        self.raw = psycopg.connect(url, row_factory=dict_row)
        self.total_changes = 0

    def _special(self, sql: str) -> Cursor | None:
        match = re.fullmatch(r"PRAGMA\s+table_info\(([^)]+)\)", sql.strip(), re.IGNORECASE)
        if match:
            table = match.group(1).strip(" `\"")
            cur = self.raw.execute("""
                SELECT ordinal_position AS cid, column_name AS name,
                       data_type AS type, (is_nullable='NO') AS notnull,
                       column_default AS dflt_value,
                       0 AS pk
                  FROM information_schema.columns
                 WHERE table_schema='public' AND table_name=%s
                 ORDER BY ordinal_position
            """, (table,))
            return Cursor(self, cur, sql)
        if re.fullmatch(r"PRAGMA\s+(?:synchronous|busy_timeout)(?:\s*=\s*[^ ]+)?", sql.strip(), re.IGNORECASE):
            return Cursor(self, self.raw.execute("SELECT 1 AS ok"), sql)
        if re.fullmatch(r"PRAGMA\s+journal_mode(?:\s*=\s*\w+)?", sql.strip(), re.IGNORECASE):
            cur = self.raw.execute("SELECT 'wal' AS journal_mode")
            return Cursor(self, cur, sql)
        if re.search(r"SELECT\s+sql\s+FROM\s+sqlite_master", sql, re.IGNORECASE):
            match = re.search(r"name\s*=\s*'([^']+)'", sql, re.IGNORECASE)
            table = match.group(1) if match else ""
            rows = self.raw.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,)).fetchall()
            columns = {row["column_name"] for row in rows}
            schema = f"CREATE TABLE {table} ({", ".join(sorted(columns))})" if columns else ""
            cur = self.raw.execute("SELECT %s AS sql", (schema,))
            return Cursor(self, cur, sql)
        if re.search(r"sqlite_master", sql, re.IGNORECASE):
            text = re.sub(r"sqlite_master", "information_schema.tables", sql, flags=re.IGNORECASE)
            text = re.sub(r"type\s*=\s*'table'", "table_type='BASE TABLE'", text, flags=re.IGNORECASE)
            text = text.replace("name", "table_name")
            cur = self.raw.execute(_replace_qmarks(text), ())
            return Cursor(self, cur, sql)
        return None

    def execute(self, sql: str, params: tuple | list = ()) -> Cursor:
        special = self._special(sql)
        if special:
            return special
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
        self.raw.execute(f"SAVEPOINT {savepoint}")
        try:
            cur = self.raw.execute(translated, params or ())
            self.raw.execute(f"RELEASE SAVEPOINT {savepoint}")
        except psycopg.errors.DuplicateColumn as exc:
            self.raw.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.raw.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise sqlite3.OperationalError('duplicate column: ' + str(exc)) from exc
        self.total_changes += max(cur.rowcount, 0)
        wrapper = Cursor(self, cur, translated)
        if re.match(r"\s*INSERT\s+INTO\b", translated, re.IGNORECASE):
            table = re.search(r"INSERT\s+INTO\s+([\w\"]+)", translated, re.IGNORECASE)
            if table:
                table_name = table.group(1).strip('"')
                has_id = self.raw.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name='id'", (table_name,)).fetchone()
                sequence = self.raw.execute("SELECT pg_get_serial_sequence(%s, 'id') AS sequence", (table_name,)).fetchone() if has_id else None
                sequence_name = sequence["sequence"] if sequence else None
                if sequence_name:
                    sequence_name = sequence_name.replace('"', '""')
                    value = self.raw.execute(f'SELECT last_value AS id FROM {sequence_name}').fetchone()
                    wrapper.lastrowid = value["id"] if value else None
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

