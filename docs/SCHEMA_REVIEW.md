# PostgreSQL schema review — 2026-09-27

## Measured baseline

Production reports PostgreSQL 18.6. There are 68 user tables, approximately
340 MiB of table/index storage and approximately 48,500 estimated dead tuples.
No invalid indexes were found. These statistics are snapshots, not evidence of
corruption or grounds for dropping objects. Only one active connection was seen
during the sampled activity query; sustained connection measurements are pending.

32 tables store one or more timestamps as text. Workflow tables use TIMESTAMPTZ
and JSONB while task/index/preflight tables retain text timestamps/payloads.
Ten foreign keys were found: mainly workflow ownership and TV draft operations.
Main/index queues and catalog projections largely depend on application-level
cleanup rather than database-enforced ownership.

Representative EXPLAIN results: media stream lookup uses media_stream_index_path;
group-stage lookup uses the unique (group_id, stage_number) index. Artifact lookup
uses a sequential scan on a small table (~70 records), which is reasonable at
this size. No speculative index changes were made on those paths.

## Priority changes and prerequisites

1. Statement-owned IDs with INSERT RETURNING (prepared). Global sequence
   last_value can identify another concurrent insert and must never link jobs.
2. Atomic queue/stage cancellation and protected retention (prepared). Add
   relationships only after inventorying and resolving existing orphan records.
3. Consolidate timestamp parsing/serialization before converting TEXT to
   TIMESTAMPTZ. Preserve API ISO strings and local-time display; quarantine invalid
   values rather than guessing timezones. Do not combine this with bulk deletion.
4. Keep searchable ownership/status/type fields typed; retain JSONB for variable
   operation definitions and evidence. Promote JSON fields only for measured
   filtering/claiming bottlenecks. Avoid indiscriminate GIN indexes.
5. Keep canonical catalog/index keys unique; review path changes and external
   sidecar ownership before introducing cascading deletes. Never cascade away
   approval/recovery records as a side effect of Plex catalog deletion.
6. Bound successful history while retaining active stages, artifacts, approvals
   and unfinished per-media LUWs. A deleted UI task must not strand a pending
   workflow or imply approval to delete its original media snapshot.
7. Measure actual worker/UI query plans and lock waits, then choose partial or
   composite indexes. Zero idx_scan alone is not proof of an unused index.
8. Confirm autovacuum health with sustained statistics before tuning. Do not run
   VACUUM FULL or REINDEX blindly; they introduce locking and extra disk needs.

## Backup blocker discovered

The image shipped pg_dump 16 but production is PostgreSQL 18.6. A requested
pre-consolidation backup failed on version mismatch. The default image now uses
PostgreSQL client 18. A replacement backup succeeded; its archive members and
pg_restore table of contents were validated before terminal-history cleanup.
A subsequent isolated PostgreSQL 18 rehearsal restored the complete database
dump successfully: 21,692 catalog rows, 798 learned corrections and 8 saved
values. Test databases and their anonymous volumes were removed afterward.
The interactive restore wizard itself was not exercised by that database test.

## Transaction round-trip optimization (2026-09-27)

The adapter previously wrapped every statement in SAVEPOINT/RELEASE, but only
handled DuplicateColumn errors. Ordinary queries now use the enclosing database
transaction directly; ALTER TABLE retains its statement guard. Temporary-table
tests confirm caught duplicate-column errors remain recoverable and ordinary
constraint failures still abort and roll back the whole transaction.

A bounded scalar-read benchmark on the configured server measured 200 reads:
old adapter 0.177 s, updated adapter 0.073 s, native psycopg 0.058 s in both runs.
This is about 59% less elapsed time for this microbenchmark, not a prediction
of end-to-end media job speed. It removes two database round trips per ordinary
statement without weakening the transaction boundary.

First-run bootstrap has now been replaced with wizard-only startup: before a
database is configured, operational routes are blocked and no schema hooks or
workers run. The ordinary connection helper is PostgreSQL-only. An isolated
container with networking disabled verifies wizard assets/status, blocked
operational APIs, and the absence of SQLite database creation. This does not
remove every SQLite-shaped SQL expression in the PostgreSQL compatibility
adapter; that conversion remains a separately tested follow-up.

Use scripts/schema_audit.py for a repeatable read-only full inventory. Findings
and proposed changes here do not imply all migrations have been implemented.
