# VideoStreamEdit redesign implementation charter

Status: approved roadmap, implementation in phases  
Current phase: Phase 6 — production hardening  
Phase 0 status: complete — compatibility inventory captured, durable configuration/learning restored, rebuildable catalog and operational state reset  
Phase 1 status: LUW foundation deployed across edits/imports and destructive-operation journals; existing full safety staging remains until final verification tests pass
Phase 2 status: protected system lanes, expiring run-sooner inheritance, and maintenance aging fairness deployed; existing priority/filter UI remains compatible  
Phase 3 status: compact filtered LUW and parent workflow/stage read models deployed; detailed lifecycle/journal endpoint remains available for review
Phase 4 status: per-media read-model freshness table, commit-driven stale marking, and generic/dedicated core-subtitle/voice/Plex worker freshness clearing deployed
Phase 5 status: shared visual tokens and opt-in status/panel/busy primitives applied across the application, including Tasks, Setup Indexes, Reports, Stream Properties, Movies/TV listings, Dashboard, Setup, Movie Import, previews, dialogs, the global busy overlay, and accessibility/responsive states; TV status polling and extended-subtitle filter queries are PostgreSQL compatible
Phase 6 status: readiness probe, constant-size operational summary, and repeatable read-only deployment smoke check are deployed; no queue or media state changes
Last reviewed: 2026-09-18  
Purpose: preserve the agreed decisions across sessions while allowing each phase to be tested and approved independently.

Baseline artifact: /home/docker/videostreamedit/data/baselines/phase0-20260917-225752.dump (checksum alongside dump; metrics manifest in the same directory).

## Product principles

- Preserve the existing user workflows and terminology wherever they already work well.
- Immediate and asynchronous changes use the same execution engine.
- Every media change is safe, idempotent, verifiable, and recoverable.
- A bulk request is an orchestrator; each movie or episode is an independent unit of work.
- Background maintenance must not block interactive editing.
- User-facing priority is controllable; internal maintenance priority remains protected.
- Visual redesign improves consistency and clarity without changing established behavior unnecessarily.
- No new feature is considered complete until its queue, failure, retry, recovery, and UI states are covered.

## Phase 0 — baseline and approval checkpoint

Before changing execution behavior:

- Export durable configuration, Plex settings, selected libraries, notes, saved values, learned values, common languages, and model configuration.
- Record current catalog counts, queue counts, failed work, index versions, and staged artifacts.
- Freeze the current behavior with representative user workflows and screenshots.
- Create a rollback point for application code and database schema.

Approval gate: the user confirms that the exported configuration and baseline tests are sufficient.

## Phase 1 — LUW execution foundation

Introduce a unified workflow model:

- One parent workflow for a user request or bulk request.
- One LUW per movie or episode.
- States: planned, preflighted, waiting, locked, applying, verifying, committed, rolled_back, failed, cancelled, and obsolete.
- One active writer per media path.
- Media signature captured immediately before each operation.
- Idempotency key for every operation.
- Parent/child progress and error propagation.
- Crash recovery from the last durable state.

Rollback storage is operation-specific:

- Metadata: old/new values only.
- Reorder: original order only.
- Rename/move: original and target paths.
- Remux/removal: atomic original-file retention until verification.
- OCR/conversion: staged source and generated output.

Approval gate: test immediate edits, queued edits, bulk edits, interruption, retry, stale signatures, and partial bulk success.

## Phase 2 — user-aware scheduler

Separate priorities into two layers.

User-controlled task types:

- Media edits.
- Imports.
- Subtitle cleanup and conversion.
- User-requested detection.
- Report repairs.

Protected system task types:

- Plex synchronization.
- Core indexing.
- Subtitle inspection.
- Language and voice detection.
- Cleanup and housekeeping.

Scheduling order:

1. Active foreground LUW.
2. User-boosted LUW.
3. Normal user LUW.
4. Plex synchronization.
5. Core index maintenance.
6. Subtitle inspection.
7. Audio/language detection.
8. Cleanup.

User controls:

- Drag-and-drop ordering of user task types.
- Run sooner for a movie, episode, show, or workflow group.
- Automatic run-sooner inheritance by child LUWs.
- Expiring boosts and fairness limits to prevent starvation.
- Filters for media, show, workflow, type, priority, and state.

Approval gate: verify that boosted work proceeds promptly, low-priority work still progresses, and no conflicting LUWs can run for the same media.

## Phase 3 — durable data and migration

Keep durable catalog and user data, but rebuild transient operational data using the new model. Historical queue/index history does not need to be preserved.

Migrate and normalize:

- Audio and subtitle track-name suggestions separately.
- Language and region usage counts.
- Combined language/region suggestions.
- Saved values versus learned values.
- Common-language configuration.
- Detection model configuration and confidence settings.
- Disabled suggestions and excluded track names.
- Usage frequency and last-used timestamps.

Remove duplicates, invalid `UNCHANGED` values, circular learned mappings, and obsolete queue records.

Approval gate: verify saved lists, ranking, language models, notes, Plex configuration, and common-language settings before deleting legacy operational tables.

## Phase 4 — indexed read model

Maintain normalized stream and media read models for fast filters and reports. Each media records independent versions for:

- File fingerprint.
- Plex metadata.
- Stream metadata.
- Subtitle inspection.
- Language detection.
- Voice detection.

Only affected consumers are re-indexed after a committed LUW. Failed Plex lookups become retryable stale records rather than silent stops or infinite retries.

Approval gate: compare filter correctness and load time against the baseline for movies, large TV shows, external subtitles, duplicate languages, HTML, image subtitles, SDH, and detection reports.

## Phase 5 — unified visual system

Create shared UI tokens and components for:

- Buttons, fields, filters, tags, dialogs, tables, navigation, status indicators, and busy overlays.
- Consistent `<<`, `<`, `>`, `>>`, refresh, close, cancel, apply, and retry behavior.
- Shared loading, progress, stale, empty, error, and recovery states.
- Responsive movie and TV two-pane layouts.
- Expandable sections instead of dense multi-column panels.
- Consistent keyboard and Escape handling.

Redesign sequence:

1. Setup and task controls.
2. Movies and TV Shows.
3. Stream Properties.
4. Reports and review dialogs.

Approval gate: user tests the same common workflows before and after redesign, with no loss of functionality or navigation context.

## Functionality preservation checklist

The redesign must retain:

- Plex sync and library selection.
- Movie, show, season, episode, and report navigation boundaries.
- Stream editing, reordering, removal, default/forced state, and external subtitle integration.
- Immediate and queued execution.
- Bulk changes with preflight filtering.
- Saved and learned properties.
- Notes, Reviewed, Plex Sync Change, attention, and detection tags.
- Subtitle inspection, HTML reports, image reports, damage reports, SDH, language, and voice detection.
- Import/move workflows and staged approval where needed.
- Retry, cancellation, rollback, cleanup, and startup recovery.

## Legacy UX compatibility inventory

The following existing interactions are explicitly protected. A redesign may change
their visual implementation, but not their meaning or availability without a new
user approval:

### Movie and TV filters

- Cascading stream type → language/region → track name behavior.
- Have / not-have / neutral filter states.
- External subtitle filtering and filename-tag filtering.
- Review, notes, Plex Sync Change, and index-status filters.
- Season filtering and filtered navigation boundaries.
- Matching counts and filter-derived bulk changes.
- Saved-value suggestions ranked by usage, with a compact initial list and “load more”.
- Filter values refreshed only when the relevant screen/filter changes, not by control flicker.

### Stream Properties

- Previous/next/first/last navigation and report/filter boundaries.
- Immediate apply, queued apply, apply-and-stay, close/cancel semantics.
- Stream reorder, remove, integrate, default, forced, language/region, and track-name edits.
- External filename tags, detection dots, confidence indicators, and hover explanations.
- Clone/learned suggestions where enabled by Setup.
- Media preview, subtitle preview, Evaluate, notes, and keyboard navigation.
- Pending-change confirmation before navigation or closing.

### Task and index controls

- Running, queued, failed, completed, and task-type filters.
- Toggle filtering by clicking the same summary again.
- Details view for every task, including plan, stages, result, and error.
- Parent workflow and child LUW visibility.
- Retry, delete, cleanup, run sooner, and cancellation controls.
- Index running/queued/failed/completed summaries with expandable sections.
- User-selected expansion state retained after refresh.

### Reports and review screens

- Movie/show report separation and correct navigation back to the originating report.
- Stream Properties navigation constrained to the report’s current result set.
- Per-item actions and bulk actions with preflight counts.
- Staged-review workflows where approval/rejection is required.
- Refresh after a committed change and removal of resolved items from reports.

### Navigation and visual behavior

- Escape closes closable dialogs and popups.
- Busy overlay explains the current operation and reports steps when possible.
- Menu and title placement remain stable while background work runs.
- Long titles, scrolling panes, and two-pane browsing remain usable.
- Button labels may be shortened for space, but their action must remain clear.

If a new design cannot preserve one of these behaviors, the implementation must
document the difference and obtain approval before merging it.

## Session protocol

At the beginning of every implementation session:

1. Read this charter and the current phase status.
2. Do not start a later phase while an earlier approval gate is open.
3. Report changed files, schema changes, migrations, tests, and known risks.
4. Preserve a short decision log entry for any deviation.

At the end of every phase:

- Run automated tests and targeted user workflow tests.
- Record performance and queue metrics.
- Document known failures and rollback instructions.
- Request explicit approval before destructive migration or deleting legacy operational data.

## Decision log

- 2026-09-17: Approved LUW/commit architecture for immediate, queued, episode, movie, show, and bulk operations.
- 2026-09-17: Approved operation-specific rollback journals instead of full-media staging whenever possible.
- 2026-09-17: Approved user-aware asynchronous priorities with run-sooner inheritance, fairness, and protected system work.
- 2026-09-17: Approved migration of learned lists, language/region usage, common-language configuration, and model configuration.
- 2026-09-17: Approved phased full UI redesign with one visual identity while preserving existing usability and functionality.
- 2026-09-17: Approved rebuilding transient queue/index state when necessary; durable user configuration and learning data must be preserved.
- 2026-09-17: Approved preserving the legacy UX compatibility inventory above, including filters, task filters, navigation boundaries, report actions, and busy-state behavior.
- 2026-09-17: Approved resetting rebuildable catalog, queue, index, workflow, detection, preview, notes, review, and per-media state; Plex configuration and learned/saved data remain authoritative.
- 2026-09-17: Phase 1 foundation deployed: durable LUW records, lifecycle events, operation journals, per-media leases, idempotency, and restart recovery are initialized in PostgreSQL.
- 2026-09-17: Regular media-edit tasks now create LUWs, record a preflight journal, acquire a per-media lease, verify, and commit or fail independently; the compatibility artifact bridge remains active during migration.
- 2026-09-17: Filtered movie and TV stream edits now use the same independent child-LUW adapter, with parent group context and a read-only LUW details endpoint.
- 2026-09-17: Movie imports now use destination-aware child LUWs and commit against the imported target signature; existing copy/stage rollback remains active until remux verification is complete.
- 2026-09-17: Destructive operations now record explicit rollback strategy in LUW journals; remux/removal, HTML cleanup, image conversion, and OCR retain original snapshots until verification/review completes.
- 2026-09-17: Scheduler policy deployed: user-requested work remains user-orderable, protected maintenance types receive a ten-minute aging allowance, and run-sooner boosts remain inherited by workflow children without becoming permanent.
- 2026-09-17: Durable LUW read model deployed with status/resource filters, summary counts, and event/journal counters so redesigned task views do not parse raw payloads.
- 2026-09-17: Parent workflow/stage read model deployed at /api/v86/workflow-read-model, exposing sequence, progress, and failure summaries without raw JSON payloads.
- 2026-09-17: Per-media read-model freshness state deployed at /api/v86/read-model-state; LUW commits mark only affected common, subtitle, language, voice, and Plex families stale.
- 2026-09-18: Successful common and subtitle index completions now clear only their own freshness family; isolated transition tests confirm independent stale flags and version increments.
- 2026-09-18: Dedicated core and subtitle index workers now clear only their own read-model family after stable successful completion, preventing stale attention tags from persisting after indexed work.
- 2026-09-18: Successful audio language detection now clears only voice freshness while retaining common-filter staleness where audio metadata affects filter values.
- 2026-09-18: Incremental Plex sync now clears Plex freshness only for changed catalog records; stream and detection families remain independently stale until their own workers succeed.
- 2026-09-18: Aggregate read-model health endpoint added at /api/v86/read-model-summary, providing constant-size stale/current counts for each index family.
- 2026-09-18: Shared visual token layer deployed as v89-design-tokens.css; existing screens remain behavior-compatible while future redesign components opt in incrementally.
- 2026-09-18: Tasks screen now opts into shared panel/status primitives while retaining filters, grouped workflows, priorities, retry, expedite, and details behavior.
- 2026-09-18: Setup Indexes now opts into shared panels and expandable queue details while preserving independent queues, controls, schedules, and expansion state.
- 2026-09-18: Corrected PostgreSQL status literals in TV attention polling; real episode smoke test now returns HTTP 200 with index reasons.
- 2026-09-18: Reports now opt into shared panel/status/button primitives while preserving report navigation boundaries, staged review actions, bulk actions, and stream-edit return context.
- 2026-09-18: Stream Properties now uses a dedicated token-based visual layer for its dialog, stream rows, editing inputs, navigation/action bar, and busy-friendly focus states without changing editing or navigation handlers.
- 2026-09-18: Movies and TV listings now use shared surfaces, filter/header treatments, pane/table states, and responsive focus styling while preserving filter cascade, paging, refresh, and navigation behavior.
- 2026-09-18: Dashboard and Setup now use shared visual tokens for collection cards/charts, setup tabs, Plex/configuration panels, saved-learning rows, and maintenance controls without changing settings or scheduling behavior.
- 2026-09-18: Movie Import now uses shared surfaces for source browsing, destination selection, filename/copy actions, and setup maintenance while preserving persistent folders, stream editing, and rollback behavior.
- 2026-09-18: Audio/subtitle preview and staged-review dialogs now use shared surfaces, navigation bars, text/image preview panels, and action states while remaining lazy and user-triggered.
- 2026-09-18: Global busy overlays now use the shared surface/ring/message treatment with fixed message height, preserving locking, percentage progress, step text, and operation-specific details.
- 2026-09-18: Generic dialogs, saved-value menus, task detail payloads, clone/template popups, and toast notifications now use shared surfaces and action states while preserving Escape, keyboard, and submit behavior.
- 2026-09-18: Added shared visible keyboard focus, status color semantics, reduced-motion handling, and compact navigation rules for small screens without changing control semantics.
- 2026-09-18: Added compact PostgreSQL/LUW/read-model readiness endpoint at /api/v86/readiness; it reports ready/degraded/not_ready without scanning media or changing queue state. The probe uses the deployed workflow_luws schema and has been verified ready in the live container.
- 2026-09-18: Queue schema startup migrations now use PostgreSQL IF NOT EXISTS guards for group/expedite columns, eliminating duplicate-column restart errors while preserving the SQLite migration path.
- 2026-09-18: Added constant-size operational summary at /api/v86/operational-summary for grouped task/index/workflow/read-model counts without payload or media scans.
- 2026-09-18: Added scripts/production_smoke.py, a dependency-free read-only check for the main page, active assets, readiness, and operational summary.
- 2026-09-18: Corrected PostgreSQL DISTINCT ordering in extended-subtitle filter values; both v51 and v53 endpoints now return sorted values without PostgreSQL errors.

## Phase 6 — production hardening

- Keep operational health checks constant-size and independent of media scans.
- Verify database, workflow schema, and read-model availability separately.
- Expose degraded state without mutating queue or media state.
- 2026-09-18: Production smoke check now validates the base health endpoint, readiness check names/status, operational-summary shape, asset non-emptiness, and per-endpoint latency; syntax and Compose validation pass against the live deployment.
- 2026-09-18: Generic and index workers now stop cooperatively on application shutdown, with bounded joins and a 90-second Compose/Uvicorn grace period; recovery smoke passed after redeploy.
- 2026-09-18: Three consecutive live smoke passes completed successfully; readiness and operational-summary endpoints remained ready/healthy with approximately 55–63 ms responses and stable asset sizes.
- 2026-09-18: Added scripts/recovery_smoke.py; it is explicitly non-mutating and verifies readiness plus operational-summary availability after a manually performed restart.
- 2026-09-18: Added scripts/approval_gate.py for read-only movie, TV, reports, dashboard, queue, and workflow endpoint validation; the live gate passed with TV listing at about 2.3 seconds.
- 2026-09-18: Added scripts/performance_gate.py; live production measurements passed: health/readiness/summary under 0.1 s, TV summary ~0.72 s, movies ~0.78 s, dashboard ~0.92 s. Recovery and functional approval gates also passed.
- 2026-09-18: Added transparent gzip compression at the shared FastAPI layer; TV listing transfer dropped from about 11.0 MB to 0.87 MB while functional approval checks remained green.
- 2026-09-18: Added compact TV show summary read model at /api/v19/tv/summary; the initial TV screen now transfers about 0.24 MB (13 KB gzip) in ~0.7 s, while full episode details load only after selecting a show. Full /api/v19/tv remains available for scoped detail loading and compatibility.
- 2026-09-18: Scoped TV detail reads now query only the selected library/show in plex_media; the selected-show detail response is about 1.0 s while the full compatibility endpoint remains unchanged.
- 2026-09-18: Added a covering catalog index for TV summary grouping and scoped show reads; no data migration is required.
- 2026-09-18: Approval gate now exercises a real summary-to-selected-show flow and verifies that scoped details contain seasons, preventing regressions in lazy episode loading.

## Phase 7 — post-foundation cleanup

- 2026-09-18: Added docs/LEGACY_INVENTORY.md as an evidence-only inventory of 134 static assets; no legacy files were deleted. Candidates require cross-reference and approval before removal.
- 2026-09-18: Repository-wide filename audit found no safely orphaned static assets; added scripts/legacy_audit.py and CI compilation guard. No cleanup deletion is justified yet.
- 2026-09-18: Added docs/MODULE_INVENTORY.md with AST-based Python route counts and cleanup rules; no module removal is justified without dynamic-import and compatibility verification.
- 2026-09-18: Removed inactive v3/v4/v6 compatibility modules and exclusive v3/v4 HTML/JavaScript assets after static reachability and cross-reference checks; shared v3/v4 CSS remains because the active bundle uses it. All gates passed after rebuild.
- 2026-09-18: Removed seven zero-reference static assets (v17 picker, v38/v40/v41 suggestions, v49/v50 preview scripts); active bundle, compatibility routes, and all gates remain healthy.
- 2026-09-18: Bulk apply-now operations now use asynchronous preflight, eliminating synchronous signature/probe delays; progress follows generated child tasks. Canonical bulk order lists no longer trigger detection, and preflight no longer invalidates detector results before a real change.
- 2026-09-18: Smart detection dependencies deployed: track-name/default/forced edits no longer invalidate language results; language/region edits target only the affected audio/subtitle indexes; removals/reorders target the affected codec family; deferred editor detection persists its scope across restarts.
- 2026-09-18: Final post-cleanup validation passed: Docker Compose configuration, recovery smoke, Python compilation, and whitespace checks are clean; production, functional approval, and performance gates remain green after the legacy removals.
- 2026-09-18: Unified smart change planner completed across immediate, queued, bulk, import, Plex-sync, conversion, OCR-restore, and compatibility edit paths. Detector follow-ups are operation-aware: track name/default/forced changes schedule none; language/region edits target only changed stream indexes; removals/integration/reorders target the affected codec family; subtitle content/conversion targets subtitles; media discovery/change targets audio and subtitles.
- 2026-09-18: Subtitle inspection now persists explicit zero-confidence findings separately from language mismatches. Movie, episode, show, and stream-property views expose a subtitle-only red X marker for unreadable, damaged, advertising, empty, or unsupported subtitle text; voice detection never produces this marker.
- 2026-09-18: Bulk apply-now and queued requests use asynchronous preflight and child task progress; unchanged media is skipped before child creation, preventing unnecessary jobs and avoiding long synchronous request stalls.
- 2026-09-18: Structural core indexing no longer launches unrelated subtitle inspection; subtitle and audio detector queues are the only detector execution paths. Targeted embedded subtitle detection excludes unrelated external sidecars.
- 2026-09-18: Legacy /api/v7/media/edit now uses the same scoped follow-up planner while optimized v43 remuxes call the internal implementation directly, preventing duplicate follow-up scheduling.
- 2026-09-18: Canonical core stream indexing now records a content signature and performs bounded signature-only stale checks, so unchanged media is not re-probed. Writes use a transactional delta snapshot and canonical rows are authoritative.
- 2026-09-18: Movie and TV filter/read paths now consume the canonical stream read model; legacy projections are compatibility-only and are no longer written unless VSE_LEGACY_INDEX_PROJECTIONS is explicitly enabled.
- 2026-09-18: Added bounded canonical-versus-legacy parity inspection and a guarded cleanup endpoint. A recent zero-mismatch, zero-missing sample is required before legacy projection cleanup; no catalog scan, rebuild, or cleanup is performed automatically.
- 2026-09-18: Setup now exposes a bounded Common filters compatibility audit with selectable media kind and sample size, plus expandable mismatch/missing-path details. The control is read-only with respect to media and never starts a full catalog run.
- 2026-09-18: Legacy projection cleanup is now blocked by design after bounded samples; it requires an explicitly complete, zero-mismatch parity audit covering the catalog.
- 2026-09-18: Added an idle resumable full-parity API (start/status/batch/cancel). It processes bounded path batches, aggregates results, and records a complete audit only after full coverage; no run is started automatically.
- 2026-09-18: Setup now exposes explicit Start, Run next batch, and Cancel controls for the resumable parity audit; the selected run ID survives Setup refresh, while no automatic batch execution is performed.
## Out of scope until the foundation is approved

- Additional OCR conversion routes.
- New detection algorithms.
- More report types.
- Further queue-specific UI variations.

These may be implemented later, but only on top of the unified workflow, scheduler, read model, and visual components.

## Phase 1 subtitle inspection foundation — approved and deployed

Approved 2026-09-18. Subtitle inspection now shares extracted text between extended health indexing and local language/SDH analysis, persists stream-level fingerprints and explicit statuses/reasons, supports precise targeted invalidation, and exposes detector health to Setup. See `docs/SUBTITLE_INSPECTION_PHASE1_PLAN.md` for objectives, acceptance criteria and follow-up phases.
