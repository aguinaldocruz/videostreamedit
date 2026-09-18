# Dead-code and legacy review — 2026-09-18

## Scope and safety rule

This review covers the deployed import chain (`uvicorn app.v86:app`), startup hooks, task-handler registration, route registration, static bundles, migrations, and preview/index compatibility tables. It is intentionally evidence-based: code was not removed merely because a symbol is not referenced locally. The full catalog, parity rebuild, preview prewarm, and subtitle-inspection production run were **not** started.

## Result

The deployed version modules form a registration chain from `v86` down through the historical layers. Several imports that look unused are required because importing a layer registers routes, startup hooks, processors, or task handlers. Those imports are now explicitly annotated so future static analysis does not mistake them for dead code.

A low-risk cleanup was applied:

- removed unused standard-library imports from `postgres_store`, `v11`, `v13`, `v28`, `v51`, `v63`, `v64`, `v65`, `v68`, and `v71`;
- removed unused locals in `v19`, `v68`, and `v74` without changing subprocess or database side effects;
- removed unused `markup_kind`/`canonical_language` imports from `v79`;
- retained side-effect imports in `v64`, `v71`, `v73`–`v76`, and `v85`, with comments explaining why they remain.

Validation: Ruff unused-import/local checks pass and `git diff --check` passes.

## Code that is live and must remain

- `app.v2`–`v19`: database, Plex, media probing, HTML/static assembly, and shared routes.
- `app.v28`, `v37`, `v38`, `v39`, `v40`, `v43`, `v48`, `v51`, `v54`, `v59`: import/edit/index/subtitle functionality and routes imported by later layers.
- `app.v63`, `v64`, `v65`, `v67`, `v68`, `v69`: preview, task queue, schedule, Plex sync, OCR/subtitle operations, and handler registration.
- `app.v70`–`v78`: startup migrations, cleanup, import refresh, and change-request routes.
- `app.v79`–`v86`: canonical index readers/writers, staged index queues, performance monitor, review routes, activity, and workflow/read-model endpoints.
- `app.v54.preview_cache_index`: still used for compatibility state, invalidation, status, cleanup, and migration paths even though new preview generation uses versioned `preview_cache_files`.
- Legacy projection writes are removed; the canonical `media_stream_index`/`media_stream_index_state` pair is authoritative.

## Compatibility-only candidates (not removed)

These are candidates for a separately approved removal after a clean deployment smoke test and a complete migration/parity audit:

1. `app.v64.preview_index_with_encoding_fallback` and its old preview processor assignment. The active path uses the v63/v69 encoder and v79 maintenance wrapper, but v64 is still the import-chain root for the queue layer.
2. `app.v70.preserve_existing_preview_samples`. It is a one-time migration hook and should be retired only after confirming every deployed database has recorded `preview_anchor_5min_v1`.
3. Legacy preview routes and subtitle-preview layers in `v49`/`v50`/`v55`. They remain reachable through the route chain and must be checked by endpoint-level smoke tests before consolidation.
4. Legacy projection table definitions and old v38/v79/v54 routes remain only as defensive compatibility surfaces for older API callers; they receive no canonical writes. They should be removed in a later API-version cleanup after endpoint usage telemetry confirms zero callers.
5. `app/main.py` and any direct legacy entrypoint. It is not used by Docker, but should be removed only after a repository-wide deployment/test reference search confirms no manual operational workflow depends on it.

## Static assets

The existing asset audit found no filename-level orphan in the active `app/static` bundle. Assets such as the v17 picker and v41 suggestion script are already tracked as removed in the current worktree; no additional asset was deleted in this pass.

## Bounded validation results

- Production smoke: passed (`/api/health`, root page, active assets, readiness, and operational summary).
- Performance gate: passed; health/readiness/summary stayed below 0.2s, TV summary 0.39s, movies 0.48s, dashboard 0.50s on the running container.
- Python compilation and Ruff unused-import/local checks: passed.
- Canonical parity sample: not executed because the currently running container returned HTTP 404 for the new parity endpoint. This indicates the container image has not yet been rebuilt from the current worktree; it is not evidence of catalog inconsistency. No restart or catalog operation was forced.

## Deployment and bounded parity results

- Rebuilt and recreated only the `videostreamedit` service; PostgreSQL, media files, and staged workflow records were preserved.
- Startup now reaches ready state after fixing the PostgreSQL-reserved parity cursor column (`offset` → `cursor_offset`).
- Staged workflows are queryable at `/api/v86/workflow-read-model`; current counts are 52,155 pending and 11,475 succeeded. The endpoint supports status filtering for focused review.
- Bounded canonical parity sample: 25 checked, 0 missing, 0 mismatches.
- Legacy cleanup remains correctly blocked because a complete parity audit has not been run.

## Full parity result

- Full audit run `#1` completed: 21,693 / 21,693 catalog media checked.
- Result: 0 mismatches, 0 missing, 0 canonical orphan rows.
- Legacy projection writes remain disabled.
- Legacy projection cleanup was executed after explicit approval: 9,278 movie stream rows, 3,008 movie media rows, 50,222 TV stream rows, and 18,684 TV media rows were removed. Canonical rows and catalog data were preserved; the deleted projection rows are not directly recoverable, but can be regenerated from canonical/media data if ever required.

## Recommended next step

Run the bounded, read-only gates in this order:

1. application compile/import smoke;
2. route/worker recovery smoke;
3. canonical-index bounded parity sample;
4. preview-cache bounded benchmark (dry run, then explicit small fixture set).

Only after those pass should the compatibility-only candidates be considered for a separate removal change. A complete parity audit and full catalog run remain explicitly deferred by request.

## Final legacy-write cleanup

After the full parity audit and explicit cleanup approval, `app/v82.unified_core_index` no longer reads or writes the movie/TV projection tables and the legacy feature flag was removed. The cleanup endpoint is idempotent for old clients, while the compatibility report now reports the canonical index as authoritative without querying legacy row counts. This change is code-only with respect to live data: canonical stream rows, catalog rows, learning data, and configuration remain intact.

The old projection table definitions/routes are intentionally retained as a short-lived defensive shell because older `/api/v38`–`/api/v80` endpoints are still registered by the versioned import chain. No active canonical path depends on their data.

## Final schema removal and catalog coverage run

- Removed the legacy movie/TV projection schema and disabled historical startup initializers; canonical stream/filter tables are now the only index source.
- Updated queue freshness checks for canonical nanosecond fingerprints.
- Migrated PostgreSQL expedite timestamps from TEXT to `TIMESTAMPTZ`, eliminating the job 241/244 comparison error. Those obsolete preparation jobs were cancelled and their looping workflow stages closed.
- Core indexing is serialized to one worker during the full-catalog run to avoid concurrent PostgreSQL write deadlocks.
- Full-catalog incremental coverage is queued in the dedicated core, subtitle-inspection, and preview-cache queues. Preview extraction is now processed by the preview worker rather than silently marked maintenance-only.

The catalog run is intentionally asynchronous and remains visible in the index queue controls.
