# Architecture

## Runtime

VideoStreamEdit is a FastAPI application served by Uvicorn. The container includes FFmpeg/FFprobe and MKVToolNix. The browser UI is delivered as bundled versioned CSS and JavaScript assets.

## Persistent state

SQLite stores Plex configuration, selected libraries, the synchronized media catalog, reusable stream values, import roots, and navigation state at `/config/videostreamedit.db`. The Plex token is encrypted with the Fernet key at `/config/plex-token.key`.

The supplied Compose file maps repository-local `./config` to `/config`. Runtime contents are intentionally ignored by Git and Docker build context.

## Media discovery

Plex supplies library metadata and the physical file paths. A synchronization stores this catalog locally so normal page loads do not query the complete Plex library. VideoStreamEdit must see those physical paths at the same container locations reported by Plex.

## Media editing

The editor probes streams and matching external subtitles, builds an FFmpeg stream-copy command, writes a uniquely named temporary file beside the source, preserves mode and timestamps, and atomically replaces the source after success. Failed edits remove the temporary output and retain the source.

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
