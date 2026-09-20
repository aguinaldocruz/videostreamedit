# Queue safety and startup recovery

VideoStreamEdit uses two queue families:

- **Generic task queue**: media edits, imports, HTML cleanup, OCR conversion, OCR rollback, Plex sync, audio detection, and index preparation.
- **Incremental index queues**: core, subtitle inspection, and preview cache work.

Media-writing tasks now use task-unique hidden temporary files and atomic replacement. A stale file from an interrupted process cannot collide with a later task. Startup checks active/failed task paths, reports unresolved OCR staging records, and removes only old application-owned temporary artifacts that are not referenced by an active task.

OCR rollback is queued as `ocr_rollback`; the web request only creates the task. The worker restores the staged original, preserves a converted snapshot, refreshes indexes, and records progress. This prevents large rollback copies from blocking the web process.

The original media is never deleted by temporary cleanup. OCR staging remains pending until the user approves or rolls back it. Failed tasks remain available for retry or deletion.

## Incremental index-stage reconciliation

At application startup, durable workflow stages whose type is `index:core`,
`index:subtitles`, or `index:previews` are reconciled against
`index_task_queue` (the generic task queue is not used for these rows). Runnable
queue items reopen cancelled/blocked stages; terminal queue items close their
stage; stages with no queue item are cancelled with an auditable reason. This
prevents a restart or migration from producing an endless “Waiting for workflow
resource or prior stage” loop. The repair count is logged as
`index_queue event=workflow_stage_reconciled`.

The performance monitor repeats this reconciliation every 60 seconds by default (override with `INDEX_WORKFLOW_HEALTH_INTERVAL_SECONDS`); preview-cache LRU maintenance remains every ten minutes (override with `PREVIEW_CACHE_MAINTENANCE_INTERVAL_SECONDS`).

A non-destructive regression check is available at `scripts/check_index_workflow_recovery.py`. It refuses to run while real index work is active, uses synthetic queue/workflow rows, verifies recovery and orphan handling, and removes its rows before exiting.
