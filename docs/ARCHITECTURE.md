# Architecture

## Runtime

VideoStreamEdit is a FastAPI application served by Uvicorn. The container includes FFmpeg/FFprobe and MKVToolNix. The browser UI is delivered as bundled versioned CSS and JavaScript assets.

## Persistent state

PostgreSQL stores the synchronized media catalog, indexes, queue state, reusable values, reports, and application settings. Protected connection credentials and encryption material live under `/config`; temporary workflow data lives under `/data`. The first-run wizard provisions a PostgreSQL database and application role, and Setup can back up or migrate the installation.

The supplied Compose file maps `/home/docker/videostreamedit`, its `data` subdirectory, and its `backup` subdirectory to `/config`, `/data`, and `/backup`. Adjust the host paths for another installation. These runtime paths are outside the repository. Backups contain database and protected configuration, not media files.

## Media discovery

Plex supplies library metadata and the physical file paths. A synchronization stores this catalog locally so normal page loads do not query the complete Plex library. VideoStreamEdit must see those physical paths at the same container locations reported by Plex.

## Media editing

The editor probes streams and matching external subtitles. Metadata-only Matroska edits use a guarded MKVToolNix path; structural edits use FFmpeg stream copying into temporary output, validate it, preserve mode and timestamps, and replace the source only after success. Failed edits retain or restore the source at the per-media logical-unit-of-work boundary.

Movie Import first copies the selected source and matching subtitles into the configured destination, applies the stream edit to the copy, and rolls back copied outputs if editing fails. Source cleanup is a separate, explicit user decision.

## Safety boundaries

- Plex media edits are authorized against the synchronized catalog and configured roots.
- Import browsing is constrained to configured input and output roots.
- System and configuration paths are blocked from browsing.
- Long-running API operations place the interface in a global busy state.
- Completed changes are written as one-line container log messages.

## Current limitations

- No built-in authentication or authorization.
- A single-process deployment is expected.
- Media edits require enough local filesystem space for a temporary rewritten container.
- Compatibility depends on the source container and FFmpeg/MKVToolNix support.
