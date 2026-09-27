"""Import bridge; saved values use the canonical schema declared in v8.

The old startup migration rebuilt this table from SQLite schema text and
discarded already-separated track names on PostgreSQL. Do not migrate learned
data during startup; any historical conversion requires an explicit backup.
"""
from app.v34 import app
