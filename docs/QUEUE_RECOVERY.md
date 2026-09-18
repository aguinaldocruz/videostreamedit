# Queue safety and startup recovery

VideoStreamEdit uses two queue families:

- **Generic task queue**: media edits, imports, HTML cleanup, OCR conversion, OCR rollback, Plex sync, audio detection, and index preparation.
- **Incremental index queues**: core, subtitle inspection, and preview cache work.

Media-writing tasks now use task-unique hidden temporary files and atomic replacement. A stale file from an interrupted process cannot collide with a later task. Startup checks active/failed task paths, reports unresolved OCR staging records, and removes only old application-owned temporary artifacts that are not referenced by an active task.

OCR rollback is queued as `ocr_rollback`; the web request only creates the task. The worker restores the staged original, preserves a converted snapshot, refreshes indexes, and records progress. This prevents large rollback copies from blocking the web process.

The original media is never deleted by temporary cleanup. OCR staging remains pending until the user approves or rolls back it. Failed tasks remain available for retry or deletion.
