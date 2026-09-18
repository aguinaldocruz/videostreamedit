# PostgreSQL persistence

VideoStreamEdit now uses PostgreSQL for its operational database. The default
Compose file starts PostgreSQL 16 and persists it under the host's
`/home/docker/videostreamedit/data/postgres` directory (mounted as `/data/postgres`
inside the application). Basic configuration and encrypted secrets remain under
`/config`; derived indexes, queues, and cache data live in PostgreSQL and were
reset during this migration.

## What was preserved

The migration snapshot preserved Plex connection settings (including the
encrypted token), selected libraries, library roots, import settings, saved
stream values, notes/review state, language settings, schedules, and learned
track-name correction history. It intentionally did not import historical media
rows, task/index queues, cache entries, or old migration history. The legacy
SQLite file was left untouched as a rollback reference.

The snapshot is created with:

```sh
python3 scripts/export_durable_config.py /path/to/videostreamedit.db /data/migration/durable-config.json
```

and imported into a freshly bootstrapped PostgreSQL database with:

```sh
python3 scripts/import_durable_config.py
```

The application then starts on PostgreSQL with no catalog rebuild automatically
queued. Media can be repopulated later through the normal Plex sync controls;
changed media will be collected incrementally.

## External PostgreSQL

The default `docker-compose.yml` uses the internal `postgres` service. For an
external server, keep `DATABASE_BACKEND=postgres`, replace `DATABASE_URL` with
the external connection string, and remove/override the application's
`depends_on: postgres` entry (the internal service can be disabled). No SQLite
fallback is used once PostgreSQL mode is selected.

## Staged workflow schema

`app/postgres_store.py` bootstraps the staged-workflow tables:

- `workflow_groups` — isolated groups of related work;
- `workflow_stages` — ordered, retryable steps within a group;
- `workflow_locks` — resource leases preventing cross-group conflicts;
- `workflow_artifacts` — staged files and rollback metadata.

The schema is initialized automatically when the application starts in
PostgreSQL mode. New generic tasks and core/subtitle/preview index tasks now
register workflow stages, acquire PostgreSQL media resource leases, enforce
stage ordering, and persist success/failure transitions. Mutating generic tasks
snapshot their original media under `/data/workflow-staging` before execution;
failed groups retain those artifacts for rollback, while successful artifacts
are marked committed. Read-only indexing and detection tasks do not copy media.

The legacy task tables remain the UI-facing compatibility queue while the
handlers are migrated. Their execution is now guarded by the staged group and
lock bridge, so queued operations cannot concurrently mutate the same media.

## Verification

A successful cutover should show:

```sh
docker compose ps
curl http://127.0.0.1:8383/api/health
```

and PostgreSQL should contain the durable settings while `task_queue`,
`index_task_queue`, and `plex_media` remain empty until the user requests a
sync. The original SQLite file should not be deleted until the new deployment
has been accepted.

## Staged workflow review and rollback

Every newly queued media-changing task is assigned a workflow group. The group
contains ordered stages, resource locks, input/output signatures, and (for
mutating media operations) an original-file snapshot under
`/data/workflow-staging/<workflow-id>/`. A task cannot pass a later stage while
an earlier stage is pending, running, or failed, and a media resource cannot be
mutated by two workflow groups at the same time.

The task queue's task-status button opens the normal task log. When a staged
group is available it also offers **Review staged workflow**, which displays the
ordered stage plan, attempts, errors, and artifact state. Failed or cancelled
groups with an original snapshot expose **Rollback staged original**. The UI
requires confirmation and the API accepts only `{"confirm":"ROLLBACK"}`; active,
pending, or successful workflows are never restored through this action.

Read-only API: `GET /api/v86/workflows/{group_id}`. Guarded restore API:
`POST /api/v86/workflows/{group_id}/rollback`. Rollback restores the staged
copy atomically, marks stages cancelled, releases workflow locks, and retains
the staged record as `rolled-back` for audit/review.
