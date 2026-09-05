# Performance and Indexing Roadmap

Last reviewed: 2026-09-04

This document is the durable performance plan for VideoStreamEdit. Review it whenever the media catalog, queue model, preview implementation, or index schema changes.

## Measured baseline

At the time of review the Plex catalog contained 3,006 movies and 18,660 episodes (21,666 media files, approximately 7.82 TB).

Observed completed-queue averages were:

- Core metadata: 0.63 seconds per medium; estimated full catalog rebuild 3.8 hours.
- Extended subtitle analysis: 4.51 seconds per medium; estimated full rebuild 27 hours.
- Preview prewarming: 4.26 seconds per medium; estimated full rebuild 25.6 hours.

The preview cache occupied approximately 1.2 GB for 3,269 indexed media and projected to 7–8 GB for the complete catalog. The old 5 GB cap therefore caused generation/eviction churn during rebuilds.

## Implemented performance release

### 1. SQLite and worker reliability

- SQLite uses WAL mode, NORMAL synchronization, a 30-second connection timeout, and a 30-second busy timeout.
- Generic queue, dedicated index workers, Plex scheduler, and index scheduler start only from the final application startup hook, after schema migrations complete.
- Setup and Docker logs expose performance risks.

### 2. On-demand preview service

- Full-catalog audio/subtitle preview prewarming is retired.
- Audio is transcoded and streamed only after the user clicks a track.
- Requested 25-second audio segments are retained in a 512 MB LRU cache.
- Cache entries older than seven days are removed from accounting and the cache is bounded by size.
- Structural media changes invalidate only the affected medium.
- Preview rebuild now means clear stored preview data and does not enqueue the catalog.

### 3. On-demand subtitle content inspection

- Scheduled/full-library HTML and styling detection is retired.
- Existing extended subtitle analysis data is removed during migration.
- Text/markup is inspected from the subtitle preview the user explicitly opens.
- Formatting-tag removal remains an explicit confirmed edit and is never inferred merely because markup exists.
- Subtitle rebuild now means clear stored analysis and does not enqueue the catalog.

### 4. Batched external-subtitle discovery

- Scheduled core checks enumerate each unique media directory once.
- The resulting sidecar list is shared across all movies/episodes in that directory.
- External filename language, region, forced and other filename tags remain part of the core index.

## Warning thresholds

The application should warn in Setup and Docker logs when:

- SQLite journal mode is not WAL.
- Core pending/running work exceeds half the catalog or 500 media, whichever is larger.
- Any index item has failed.
- Any generic media-operation job has failed.
- Preview storage reaches 90% of its configured limit.

Also treat these as review triggers even if not yet automated:

- Core average processing time exceeds 1.5 seconds per medium.
- Core rebuild estimate exceeds eight hours.
- A scheduled sidecar scan exceeds ten minutes.
- Interactive preview startup regularly exceeds five seconds.
- Database-lock errors reappear.
- Preview cache hit rate stays below 20% after sufficient real use; consider disabling disk caching.
- Preview cache hit rate exceeds 70%; consider allowing a modestly larger cache.

## Deferred roadmap

Do not implement these until overall performance is measured after this release:

1. Unify movie and TV stream tables into one schema with source, codec, language, region, track name, default, forced, position, external filename tags, and an index-version fingerprint.
2. Use `mkvmerge -J` as the single primary metadata probe for Matroska media and FFprobe as the fallback for other containers.
3. Consolidate the three external-sidecar state rows per media into one compact record with per-consumer versions.
4. Add automatic retention/aggregation for old successful queue history.
5. Add a single resource governor that pauses low-priority probing while edits, imports, or interactive previews are running.
6. Cache requested internal subtitle text pages and graphical subtitle event timestamps only after access.

## Review method

Compare future measurements against the baseline above. Prefer reduced work over additional concurrency: avoid full-media reads, repeated directory listings, eager transcoding, and duplicate probes. Interactive edits and previews always take priority over maintenance work.
