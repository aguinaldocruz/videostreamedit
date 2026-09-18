# Preview cache redesign — production review

Status: proposal for approval; no catalog run started
Reviewed: 2026-09-18

## Executive conclusion

Preview caching should remain on-demand. A full-catalog preview prewarm is not justified: it consumes CPU and disk for media the user may never inspect. Subtitle preview caching should remain removed; text subtitles are read from the media when requested. Audio samples should use one consistent encoder and a bounded, deduplicated cache.

## Current behavior and findings

- The active audio preview path is the `/api/v69/stream-preview/audio` endpoint. It defaults to the five-minute anchor and moves in 25-second segments.
- A cache miss launches FFmpeg and stores the requested segment. Concurrent misses for the same stream/segment can still perform duplicate extraction.
- Cache metadata is stored in `preview_cache_files`; LRU enforcement scans all rows after a new segment. This is acceptable at small scale but becomes increasingly expensive as cache entries grow.
- The cache is limited to 512 MiB by default and expires entries after seven days. The limit is configurable, but there is no explicit free-disk guard or per-media quota.
- Cache files live below the configuration cache directory. The database/data split should eventually place transient audio cache under `/data`, leaving `/config` for durable settings and learned values.
- `duration.txt` is written during preview-info requests, and duration may be probed repeatedly when it is absent. Duration belongs in the media preview metadata record.
- The preview index worker has legacy prewarming implementations in older modules. The on-demand policy clears old prewarmed state, but the implementation has several historical encoder paths and should be consolidated before production.
- Legacy paths use different audio encodings (mono/64 kbps versus stereo/128 kbps), so size and quality are not uniform.
- Invalidation removes cached files for media changes, but cache state is not independently fingerprinted against the actual media content. Mtime/size and operation invalidation should be supplemented by the canonical media signature.
- Subtitle preview cache removal is correct for storage control, but the UI and setup terminology must not imply that subtitle samples are prewarmed.

## Recommended target design

### 1. On-demand only

Retire preview catalog indexing as a media-processing job. Keep a lightweight status/read model only if Setup needs counts. A preview “check” should verify cache consistency and orphan files, not probe every media file. “Rebuild preview cache” should become “Clear audio preview cache” and require explicit confirmation.

### 2. One encoder contract

Use one preview encoder for interactive and any optional warm requests:

- 25-second segment;
- start at five minutes by default, with first/last navigation still available;
- mono, 32 kHz, 64 kbps MP3 (or an equivalent small format);
- `-nostdin`, bounded timeout, low process priority;
- atomic temporary file replacement.

The encoder should report duration, actual segment, codec, and extraction time. Do not transcode the full media.

### 3. Request deduplication and resource governance

Add a per-cache-key lock/future so simultaneous requests for the same media, audio stream, and segment share one extraction. Limit concurrent FFmpeg preview processes globally (default one, configurable at most a small number) and yield to foreground media edits/imports. Preview requests must never block index workers or user edits.

### 4. Durable cache metadata and signatures

Store, per cached segment: media path, canonical media signature, audio stream identity, segment number, byte size, duration, last access, creation time, and encoder version. A signature mismatch makes the entry stale and deletes only that media’s samples. Encoder upgrades invalidate old segments by version rather than requiring a catalog rebuild.

### 5. Bounded storage policy

Keep a global byte limit and seven-day idle expiry, but enforce incrementally rather than scanning every row on every request. Add:

- a free-disk safety floor;
- per-media and per-stream segment caps;
- cleanup of missing/orphan files;
- a periodic housekeeping task with a long interval;
- cache accounting reconciliation on demand.

The cache should be disposable. If cleanup or corruption occurs, the next preview simply regenerates the segment.

### 6. Metadata without repeated probes

Persist duration and audio-stream count from the canonical stream metadata/read model when available. Only probe the media on demand when metadata is absent or stale. `stream-preview/info` should be read-only and must not write a file on every request.

### 7. Setup and user experience

Replace preview-index controls with a compact Audio preview cache block:

- enabled/on-demand indicator;
- current bytes, limit, hit rate, misses, active extractions, and last cleanup;
- cache limit editor;
- “Clean expired/orphaned entries”;
- “Clear all audio preview cache”;
- optional “Disable disk cache” for memory-constrained servers;
- no full rebuild button and no subtitle-cache controls.

Preview UI should show cache hit/miss, segment time, duration, and a short busy message only while that requested segment is being extracted.

### 8. Observability and failure handling

Record bounded counters and latency percentiles rather than per-request verbose logs. Log extraction failures with media, stream, segment, duration, and a short FFmpeg error. A failed preview must not create an index failure or retry loop. Corrupt/zero-byte files are removed on read and retried once.

## Performance/resource impact

Expected improvements:

- no full-catalog preview CPU or disk cost;
- no duplicate FFmpeg work for concurrent identical requests;
- lower bytes per segment and consistent storage accounting;
- no full-table LRU scan per request;
- fewer media probes through persisted duration metadata;
- preview work isolated from core/subtitle/language queues;
- predictable upper bound on cache storage and FFmpeg concurrency.

The trade-off is that the first preview click remains a short extraction wait. This is preferable to reserving storage for unused media.

## Phase 1 implementation status

- Shared preview constants now define the five-minute anchor, 25-second segments, mono/32 kHz/64 kbps encoding, and encoder version `audio-preview-v2`.
- The active v69 on-demand endpoint and v63 cache endpoint use the same command contract.
- Responses expose the encoder version for diagnostics.
- No preview indexing, prewarming, catalog scan, or cache rebuild was started.

## Phase 2 implementation status

- Added a per-media/audio-stream/segment claim so concurrent identical requests reuse the first generated segment.
- Added a bounded preview extraction semaphore (one FFmpeg preview process by default).
- A waiting request rechecks the cache after acquiring its claim and avoids duplicate extraction.
- No queue, index, catalog, or prewarm work was started.

## Phase 3 implementation status

- Cache rows now carry media signature, encoder version, duration, and creation time.
- Existing rows without verifiable metadata are treated as stale and regenerated only when requested.
- Preview responses expose the encoder version and persist duration with newly generated samples.
- The media signature is bounded to file metadata plus first/last content windows; no full-media read is performed.
- No catalog scan, prewarm, rebuild, or cache clear was started.

## Phase 4 implementation status

- Disposable preview files now use `DATA_DIR/preview-cache` (normally `/data/preview-cache`).
- Existing files under the old `/config/preview-cache` location are moved once when the new location has no same-named entry; media is never scanned.
- Cache housekeeping now honors a configurable free-disk floor (`PREVIEW_MIN_FREE_GB`, default 1 GiB), expires idle entries, and removes database rows for missing files.
- Preview settings report the active and legacy directories for operational clarity.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 5 implementation status

- Preview media processing is now maintenance-only: new media/index requests invalidate affected samples but do not enqueue per-media preview work.
- Existing legacy preview queue rows complete as explicit maintenance-only skips without probing media.
- Legacy preview schedules are retained for compatibility but are not started by the scheduler.
- Added explicit `/api/v63/setup/preview-cache/maintenance` reconciliation and periodic housekeeping from the performance monitor.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 6 implementation status

- Setup now reports cache file count, bytes used/limit, free disk, free-disk floor, and on-demand mode.
- Added explicit “Clean expired/orphaned entries” and “Clear audio preview cache” actions.
- Clearing removes disposable preview samples only; it does not enqueue index work or touch media/catalog data.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 7 implementation status

- Added bounded runtime metrics for hits, misses, hit rate, extraction count, extraction latency, and failures.
- Setup displays hit rate and average extraction time beside storage health.
- Metrics reset only when the disposable cache is explicitly cleared.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 8 implementation status

- Added `scripts/preview_cache_benchmark.py` for explicit-path cold extraction and bounded concurrency measurements.
- The benchmark defaults to three files, two segments, and one worker; it cannot discover the Plex catalog.
- Duplicate-request pressure can be measured explicitly with `--duplicate`.
- The tool was validated syntactically but not executed.

## Phase 9 implementation status

- LRU and orphan housekeeping now process bounded batches of at most 512 cache rows per pass instead of loading the entire cache index into memory.
- Expired entries are prioritized; over-limit cleanup removes oldest entries incrementally.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 10 implementation status

- Added `PREVIEW_MAX_CONCURRENT`, capped from 1 to 4 and defaulting to 1.
- Setup reports the active concurrency cap alongside cache health.
- The default remains conservative until benchmark measurements justify increasing it.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 11 implementation status

- Setup cache health now also shows extraction count and preview failures, making benchmark and production regressions visible without log inspection.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 12 implementation status

- Docker Compose now exposes `PREVIEW_MAX_CONCURRENT` and `PREVIEW_MIN_FREE_GB` with conservative defaults.
- Production tuning can be changed through environment configuration and remains capped by application safety limits.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 13 implementation status

- Added `scripts/preview_cache_gate.py`, a read-only acceptance gate for saved benchmark reports.
- The gate checks completed-job accounting, failure rate, average extraction latency, and average sample size.
- It never invokes FFmpeg or discovers media.
- The benchmark can now persist its report with `--output`, allowing repeatable gate evaluation.
- Added `--dry-run` to validate selected fixtures and generated commands without invoking FFmpeg; dry-run reports are not production acceptance reports.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 14 implementation status

- Added `docs/PREVIEW_CACHE_BENCHMARK_PROTOCOL.md` with bounded fixture selection, cold/warm/duplicate measurements, resource checks, and initial acceptance thresholds.
- The protocol explicitly forbids unbounded library scans and preview prewarming.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

## Phase 15 implementation status

- CI now syntax-validates the bounded preview benchmark and acceptance-gate tools.
- This protects the readiness workflow without executing FFmpeg or scanning media.
- No catalog scan, preview prewarm, rebuild, or cache clear was started.

Example after selecting a small fixture set:

```bash
python scripts/preview_cache_benchmark.py /media/test/sample.mkv --limit 1 --segments 2
```

## Implementation phases

1. Consolidate active preview routes and encoder settings; mark legacy prewarm code as compatibility-only.
2. Add cache-key locks, encoder-versioned metadata, duration persistence, and atomic corruption handling.
3. Move transient cache to `/data`, retain settings in `/config`, and add incremental LRU/orphan cleanup.
4. Remove preview media work from catalog index scheduling; retain only read-only cache maintenance.
5. Redesign Setup controls and preview status metrics.
6. Benchmark cold/hot/concurrent previews on representative codecs and verify no impact on edits or indexing.
7. After approval, optionally clear existing preview artifacts; do not run a catalog prewarm.

## Acceptance gates before production

- Cold, hot, and concurrent preview requests are measured.
- Duplicate extraction is eliminated for the same key.
- Cache stays below configured bytes and free-disk floor.
- Media changes invalidate only affected samples.
- Subtitle preview never creates cache files.
- Preview failures do not enqueue or block unrelated jobs.
- Setup status remains responsive with a large cache index.
- No full catalog preview run is required.
