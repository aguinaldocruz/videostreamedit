# PostgreSQL persistence and recovery

## Current deployment

The default Compose deployment runs the application with an **external
PostgreSQL server**. It does not start a PostgreSQL container and does not store
database passwords in Compose. Configure a new installation through the first-run
wizard; migrate an existing installation from **Setup → Tasks → Migrate**.

The encrypted connection configuration and its encryption key live under
`/config`. Keep them private. Application data, indexes, settings and learned
values are stored in PostgreSQL; transient workflow files live under `/data`.
The optional single-container profile is not the production default and must not
run alongside the default application using the same persistence.

### Optional single-container profile

Build the default application image first, then the optional image:

```sh
docker compose build videostreamedit
docker compose --profile single-container build videostreamedit-all-in-one
```

Only start `videostreamedit-all-in-one` after stopping the default application.
It inherits the same application/tools, adds PostgreSQL 18, and supervises both
processes. A fresh local database uses a randomly generated application password
stored in encrypted configuration. Local administrator access uses Unix peer
authentication; TCP uses SCRAM and listens only on container loopback.

An existing encrypted external connection is respected and no local database is
started. An existing local cluster from another PostgreSQL major version is
refused: use a logical backup/restore into a fresh directory. Never delete or
rewrite PG_VERSION to force an upgrade. The profile allows 125 seconds to drain
the application and cleanly stop PostgreSQL.

`scripts/test_all_in_one.py` validates fresh initialization, full database-dump
restore in an isolated target, credential reuse and graceful shutdown. It mounts
only the selected backup directory read-only and removes its throwaway volumes.

## Backup and migration

Use **Setup → Tasks → Backup** to create or restore a portable backup.
The default host backup directory is
`/home/docker/videostreamedit/backup`, mounted at `/backup`.
The migration wizard can provision the destination and restore to another
PostgreSQL server, then switch the encrypted connection.

The default image includes PostgreSQL client 18. Backup tools must support the
server version: an older pg_dump cannot dump a newer server. Verify the backup
archive and pg_restore table of contents before destructive maintenance; a
complete restore rehearsal is a separate validation step.

The old SQLite export/import scripts describe the historical redesign migration,
not the procedure for moving the current live PostgreSQL database. Do not use
them for routine backup or server migration.

## Workflow ownership

A movie or episode is the media transaction boundary. A bulk request does not
require keeping the originals for an entire TV show until every episode finishes.
The schema records groups, ordered stages, media resource leases, transaction
journals, input/output signatures and artifact ownership. Read-only indexing and
language detection do not require full-media snapshots.

Transient originals are stored under `/data/workflow-staging/<workflow-id>/`.

- Active work keeps the recovery data required for its current transaction.
- Successfully completed workflows release their transient recovery files.
- Failed workflows/jobs retain recovery data while available for review/retry.
- Deleting failed job history records a durable discard intent and releases its
  recovery data when no active or retained failed sibling still needs it.
- File-removal failures retain ownership records for a later cleanup attempt.
- User-approved conversion/download staging is a separate lifecycle and is not
  removed by transient workflow cleanup.

Deleting recovery data is irreversible unless another independent copy exists.
It does not delete the current library media.

## Review and verification

The task-status button opens task details and available workflow information.
`GET /api/v86/workflows/{group_id}` returns the workflow details.
Rollback is only possible while an eligible original snapshot still exists; it
is not available for successful workflows whose temporary originals were removed.

After deployment, check `docker compose ps`, `/api/health`, startup logs and
queue state. No full catalog rebuild is required for a storage cleanup.
