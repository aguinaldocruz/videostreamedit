# Scheduled tasks: manual runs and latest logs

Setup → Tasks → Scheduled Tasks puts **Cache subtitles** first. Every task card
has **Run now** and **Latest log**, including Plex sync, core metadata discovery,
subtitle inspection discovery, subtitle/voice language detection dispatch,
system backup, and preflight history cleanup.

Run now uses the task's saved settings, even if its recurring schedule is
disabled. Cache runs retain their configured time budget and graceful-stop
control. Index/detection requests use the durable light queue; repeated requests
deduplicate active work. Backups retain their existing exclusive-operation guard.
Cleanup applies the saved retention age and never removes active work or media.
Changing unsaved form fields does not change a manual run's configuration.

## Reading a log

Logs show the request source, timestamps in the configured application timezone,
current state, a compact summary, informational steps, affected media paths,
and errors. Discovery/dispatch completion means that work has been **queued**,
not that the downstream indexing or detection has completed. Linked job entries
show those workers' current states and errors when opened/refreshed.

The popup stays open while reading. Refresh reads current results; Load earlier
keeps pagination on the same run even if a new run starts. Text is escaped and
connection passwords/tokens are masked. No raw payload JSON is displayed.

`scheduled_job_run` and `scheduled_job_event` store compact metadata, not media
or subtitle contents. Each task retains its latest 10 finished runs (active runs
are protected) and up to 5,000 recent detail entries per run; the summary covers
the whole run. Cache fast-skip pages are summarized together. Existing cache
failure tracking and queue error/retry controls remain unchanged.

Detailed recording begins with runs executed after this change; old Docker
console output is not reconstructed. After an interrupted process restart,
direct-run logs are marked interrupted and queued-task logs follow the durable
queue's actual recovered state.

## Checks

- `python3 scripts/test_scheduled_job_log.py`
- `python3 scripts/test_scheduled_dispatch.py`
- `python3 scripts/test_subtitle_cache_schedule.py`
- `python scripts/test_scheduled_job_postgres.py` (isolated PostgreSQL schema;
  application image with encrypted configuration mounted read-only)
- `python scripts/test_scheduled_task_ui.py` (Playwright; isolated fake API)

These checks do not start a full-catalog run or modify production media.
