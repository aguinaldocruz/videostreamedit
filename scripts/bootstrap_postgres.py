#!/usr/bin/env python3
"""Bootstrap the clean PostgreSQL schema without touching media or indexes."""

from app.postgres_store import initialize_schema


if __name__ == "__main__":
    initialize_schema()
    print("PostgreSQL workflow schema initialized")

