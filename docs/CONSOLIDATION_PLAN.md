# Approved consolidation and PostgreSQL review

Preserve configuration, encryption keys, learning, models, active drafts,
pending approvals and recoverable per-media originals. No full catalog reset.

## Execution gates

1. Safety: remove destructive saved-value migration; statement-local inserted
   IDs; retain records on artifact deletion failure; regression tests.
2. Back up database and protected configuration before deployment/migrations.
   Check existing backups for lost learned names without overwriting current data.
3. Review all schema types/defaults, constraints, foreign keys, indexes, query
   plans, transactions/locks, connections, autovacuum, retention and JSON usage.
4. Coordinate task/index deletion with workflow stages, groups and artifacts.
   Resolve abandoned work and validate commits/rollback per media before cleanup.
5. Garbage collection: dry-run ownership/reason/bytes manifest; recheck claims
   before deletion; preserve approvals and recovery evidence on partial failure.
6. Native PostgreSQL access; ordered schema/recovery/worker startup; replace
   obsolete projection routes before removing import bridges.
7. One UI owner per Setup panel/editor/report/dialog/busy operation; dispose
   timers/listeners; preserve drafts, direct edits, navigation, filters and overflow.
8. Explicit asset manifest; prove reachability before removing assets/middleware;
   refresh legacy inventory and deployment documentation.
9. Test retry/cancel/restart, learning, concurrent IDs, reports, templates,
   direct/draft editing, backup/restore and measured bounded performance.

## Baseline (2026-09-27)

~34 GiB referenced workflow staging; most failed/pending groups have no main
queue task. ~71,600 succeeded groups retained. Local retired PostgreSQL directory
~494 MiB. 65 substantive Python modules reachable; nine asset middleware layers.
Nine static asset candidates lack Python filename references. Setup browser
checks found no duplicate IDs or leaked dialogs in the exercised sections.

## Progress

Safety changes tested: destructive saved-values migration removed; INSERT
RETURNING IDs; cleanup retains failed records and rejects escaped paths; pending
task cancellation updates its exact workflow stage in the same transaction;
history pruning protects unfinished stages and artifact-bearing groups.

Database/client mismatch discovered (server 18, pg_dump 16). Default image upgraded
to client 18; SSL connection options use libpq environment parameters. Protective
backup `/backup/videostreamedit-backup-20260927-154037.tar.gz` created; archive
members checked and pg_restore --list succeeded (323 lines). Older two backups
contain language/region values only, no saved title_audio/title_subtitle rows.

Mock safety suite and real PostgreSQL temporary-table ID regression passed.
Full schema review findings are in SCHEMA_REVIEW.md. Other gates remain pending;
these safety fixes do not imply the entire schema/UI consolidation is complete.

Cleanup checkpoint (2026-09-27): with explicit user authorization, deleted the
two obsolete September 23/24 backup archives (58,950,718 bytes). Kept the verified
September 27 backup above. Removed 69,650 obsolete succeeded/cancelled workflow
groups in bounded transactions; retained recent history, queue references,
unfinished stages, claims and every artifact-bearing group. No staged media file
was deleted. Those originals require per-media recovery validation, not deletion
based solely on a failed/pending status. Eight unreferenced static source assets
were removed; see LEGACY_INVENTORY.md for the reconciled list.

The default production image contains the safety fixes and PostgreSQL client 18.
Source asset deletions and the schema initializer's LUW group index addition
are now deployed. The missing LUW group lookup index
was identified using EXPLAIN; live creation uses CONCURRENTLY to avoid an
exclusive table rebuild. No full catalog rebuild was launched.

Recovery-policy deployment: failed-job deletion now records durable recovery
discard intent in the same database transaction. Cleanup protects live work and
retained failures, retries filesystem errors, and does not touch approval staging.
Real PostgreSQL temporary-table tests cover success, failure, deletion, shared
ownership and active locks. Startup reclaimed 32 abandoned snapshots totaling
12,811,401,478 bytes; 38 failed-workflow snapshots remain (23,658,249,970 bytes).

Index consolidation: obsolete movie/TV projection writers, background movie
index threads, the extended-index migration reset, and retired preview startup
hook were removed. Existing API URLs now use canonical queue/processor paths.
Movie filter values are scoped to movies. Read-only live-database smoke tests
and routing, safety, queue-retry and language-detection regressions passed.
Deployed after active HTML-cleanup task 12118 succeeded. Health/status/filter
HTTP smoke checks passed; the queue was resumed. Reports browser regressions
passed with no JavaScript errors. No full catalog rebuild was requested.
Full native PostgreSQL conversion and UI lifecycle consolidation remain pending.

The stream-editor browser suite now passes direct/queued apply, draft edits and
navigation, cleared pending changes, and narrow-screen scrolling. The test's
draft-session GET response was isolated from real server polling; no media writes
were sent. Adapter transaction guard tests and measurements are recorded in
SCHEMA_REVIEW.md. Keep first-run bootstrap behavior until its replacement is
implemented and verified; it is an active feature, not an unused SQLite fallback.

Transaction-overhead optimization deployed after all active media jobs drained.
Ordinary reads/writes no longer issue redundant SAVEPOINT/RELEASE statements;
schema ALTER guards remain. The 200-read microbenchmark improved from 0.177 s to
0.073 s; real PostgreSQL rollback/constraint tests passed. No catalog reprocessing
was required. Remaining follow-up includes typed timestamp/JSON migration,
first-run/native-access consolidation, learned-schema migration safety, and
single-owner Setup/editor/dialog lifecycle cleanup with browser regressions.

## Runtime consolidation deployed — 2026-09-27

- Corrected the language-detection flush endpoint: its decorator was attached
  to the index-worker shutdown function. Regression verifies the exact handler.
- One web-asset owner replaces nine middleware layers. JavaScript/CSS bundles
  are cached per process and support ETag/304 revalidation; HTML remains no-store.
  Branding and manifest routes remain available.
- Workflow schema/recovery runs before setup hooks; dispatcher, worker and
  backup scheduler startup follows setup. Production startup completed without
  Python/SQL errors in the inspected startup log.
- Learned correction schema upgrades preserve mappings, counts, language keys
  and disabled values. Removed destructive/redundant upgrade hooks. Tests cover
  old-schema upgrades, idempotency and uniqueness using isolated PostgreSQL
  schemas rolled back afterward, plus an in-memory legacy fixture.
- First access without credentials is wizard-only, with operational APIs blocked
  until configuration and restart. Network-disabled fresh-install tests pass.
- Deployed after pausing claims and confirming no running media task; restored
  the queue to unpaused. API/assets/branding smoke checks pass. Reports and
  stream-editor browser fixtures pass with no real media writes.

No full catalog scan was started. No configuration, learning, approval staging
or failed-workflow recovery files were discarded in this deployment.

## Remaining consolidation gates

These are not declared complete by the runtime deployment:

1. Shared timestamp/JSON serialization contracts followed by incremental native
   PostgreSQL conversion. Preserve API shapes and test every converted consumer.
2. Full restore rehearsal in an isolated target database (archive validation
   alone is not a restore test).
3. Optional all-in-one image parity/security/supervision validation; production
   uses the external PostgreSQL image, not that optional profile.
4. Further single-owner UI lifecycle extraction and integration regressions.
   A reachable versioned module is not automatically dead code.

## Highest-value subsequent improvements

- **One activity subscription per browser:** replace overlapping queue/status
  polling with a shared, visibility-aware client and eventually server events.
  Keep polling fallback and refresh after reconnect. Measure requests/minute,
  database reads and status-update latency before and after.
- **Bounded connection pooling:** reuse PostgreSQL connections rather than
  reconnecting for short reads. Budget across all workers/processes, with
  acquisition timeouts and separate foreground/background limits. Benchmark
  tail latency under concurrent browsing and jobs before enabling globally.
- **One operation-result contract:** every direct, queued and draft edit returns
  the accepted revision, affected tracks and refresh requirements. The editor
  should clear only acknowledged changes and ignore stale responses. This
  directly addresses repeated pending-change and stale report regressions.
- **Measured background resource budgets:** limit disk-heavy conversions
  separately from metadata/index work; reserve capacity for foreground reads.
  Use observed disk pressure and job timings, not a fixed increase in threads.
- **Incremental report projections:** update affected media after commit instead
  of rebuilding broad report availability on every opening. Revision-bound
  invalidation must preserve final-version exclusions and pending-work hiding.

These recommendations are not claims of implemented features. Optimize using
bounded representative fixtures, not another full catalog run.

## Follow-up cleanup and optional validation — 2026-09-27

Completed and verified:

- Removed unused `v7-addon.js`, `v81-performance.js`, root `old-v79.js`,
  import-only `v15.py`/`v70.py`, and the unused asset-response helper. Redirected
  import callers rather than losing side-effect registrations.
- Converted remaining runtime PRAGMA/sqlite_master checks to PostgreSQL schema
  queries; removed fake SQLite schema/WAL responses, SQLite exception wrapping,
  and unused DB_PATH. SQL placeholder/upsert translation remains actively used.
- Removed historic core-index reset/parser migrations and preview-removal
  migration code. Startup now creates canonical schema before queue recovery.
  It cannot run those old full-catalog rebuild/reset migrations again.
- Removed empty `performance_metric`, retired `tv_stream_index_settings`
  containing only a format-version marker, unused `cursor_cursor_offset`, and
  duplicate `media_video_title_path`/`track_name_correction_language_lookup`
  indexes. Removed the now-unreferenced `feature_migrations` history table.
  Cleanup script validates exact data/index equivalence and uses no CASCADE.
- Performance monitor now stops during shutdown; runtime health checks cannot
  clear the index shutdown flag and resurrect workers. Database connect attempts
  are bounded to 10 seconds. Core coverage uses catalog-scoped index-state rows.
- Fresh volume mount roots receive application ownership without recursively
  changing PostgreSQL directory ownership.
- Optional all-in-one image now inherits the current application image and uses
  PostgreSQL 18, generated encrypted app credentials, peer/SCRAM authentication,
  loopback binding, bounded startup, and coordinated app/database shutdown.
  Existing encrypted external connections do not start a local database.
  Existing clusters of another major version are refused, not upgraded blindly.
- Full database dump restored into isolated PostgreSQL 18 with networking
  disabled and no live media/config/data mounted. Restore counts: 21,692 catalog
  rows, 798 learned corrections, 8 saved values. Restart reuses encrypted
  credentials; graceful shutdown confirmed. Test container/volumes removed.
  This validates the database dump, not every interactive restore-wizard path.

Protected exceptions: migration JSON snapshots contain 158 historical saved
values whereas current reusable values contain 8. They are retained pending
user review/merge choice, not classified as disposable. The retired PostgreSQL
16 directory is also retained until its learned-data contents can be verified.
Failed-workflow recovery and unapproved subtitle staging remain protected.

Timestamp column conversion, replacing every actively used SQL translation,
and event-driven UI/pooling proposals are architectural improvements, not
proven-dead code cleanup. They must not be represented as completed or justify
dropping working data structures in this cleanup pass.
