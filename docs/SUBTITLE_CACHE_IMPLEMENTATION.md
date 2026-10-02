# Complete text-subtitle cache implementation

The cache covers complete text-based embedded and external subtitles. It does
not OCR graphical subtitles. The media remains authoritative; cached text is
usable only with a matching source signature and a complete track manifest.

## Phases

1. **Storage and consistency foundation (implemented).** PostgreSQL media
   manifests and per-track text, atomic complete-set publication, signature
   checks, checksums, and per-media invalidation. The scheduled pass trusts
   complete cache records instead of re-fingerprinting every cached media.
2. **Extraction and incremental work selection (implemented).** Identify
   eligible text tracks, derive a media/sidecar signature, extract full text,
   and recheck the source before publication. Complete-media extraction skips
   a valid cache; a Media Review miss publishes only the selected track and
   correctly marks the media partial. The candidate query orders final-revision
   media first and then other media, newest Plex additions to oldest within
   each group. Source signatures still guard extraction and direct cache reads;
   core indexing and Plex change detection own scheduled-work invalidation.
   When two or more embedded text tracks are missing, one FFmpeg invocation
   demuxes the media into separate temporary SRT files. Every output is
   checked; a failed batch is discarded and retried per track to isolate the
   error. The complete cache is published only after all tracks and the source
   signature pass validation. Temporary SRT files are removed automatically.
3. **Scheduled bounded worker and controls (implemented).** Setup → Tasks →
   Scheduled Tasks now has frequency, local start time, HH:MM run budget, Run
   now, graceful Stop, and live progress. The schedule defaults to disabled;
   no full-catalog run starts on deployment. A PostgreSQL advisory lease
   prevents overlapping workers. The worker persists its catalog cursor,
   selects missing/incomplete records in database batches, skips indexed media
   without subtitles, and stores per-run counts/errors. Marking a movie,
   episode, or whole TV show Final Revision promotes uncached media ahead of
   ordinary changed-media work and the saved catalog cursor; this also covers
   finalization inside a TV-show draft. Existing complete caches are not
   extracted again. It stops
   claiming work at the deadline or cancellation; an incomplete current
   media is never published. Extraction runs at low CPU priority with a
   per-track timeout bounded by remaining run time. Restart marks an
   abandoned run interrupted and resumes from the saved cursor. Database
   capacity monitoring remains a production hardening item in phase 5.
   Per-media failures are retained for review and are not retried unchanged.
   Stream Properties opens directly from the failure list; opening Final
   Revision media removes that flag (or stages its removal in a TV-show draft)
   so editing is available. A detected
   media change clears the failure and queues recaching; explicit Retry this
   media and Retry all actions do the same without requiring a media change.
   Viewing a failure alone does not retry it. Historical run
   totals from before per-media failure tracking remain in run history but
   cannot reconstruct a complete failure list.
4. **Consumers and editing workflow (implemented).** Media Review,
   interactive/scheduled language analysis, and HTML preflight use valid cached
   text when available. Background subtitle inspection processes only media
   with a complete, source- and checksum-verified cache. Uncached or partial
   media remains pending (shown as waiting for subtitle cache) for a later
   inspection pass; inspection never extracts text as a fallback. Embedded HTML
   cleanup derives a clean
   subtitle from cached text, remuxes to a temporary media file, verifies stream
   count, cue count, dialogue and absence of HTML, and only then replaces the
   original. The verified final text is published under the new media
   signature, with other tracks marked uncached. External SRT cleanup uses the
   same cached-input/verified-output pattern; other external text formats keep
   their native-format edit path and refresh a normalized cache afterward.
   Failed output verification leaves the original untouched. A cache database
   write failure after a verified media commit leaves the cache invalid rather
   than making the completed edit retryable. Core index refreshes use their
   stream and sidecar change results to retire obsolete cache entries and add
   changed media to a priority recache
   queue. HTML edits likewise queue the remaining tracks of that media;
   completed recaches remove their queue entry, while failures receive bounded
   retry backoff. Newly discovered Plex media join this priority queue after
   an existing catalog is established. Future subtitle editors can reuse this
   per-media validation boundary.
5. **Production verification (pending).** Exercise changed files/sidecars,
   media edits, finalized media, cancellation, restart, stale-source races,
   and mixed codecs; pilot a small catalog subset before enabling the full
   incremental pass. Measure DB/WAL growth and extraction throughput, guard
   database capacity, and measure the priority queue during a long cursor
   pass. Plex sync invalidates old copies for changed files; core indexing
   queues only media with subtitle streams after it completes. Newly indexed
   subtitle-bearing media is queued by that same core-index hook. A resumed
   run uses the saved cursor plus explicit
   change requests; it does not infer changes from timestamps or re-fingerprint
   cached media.

Final-revision status grants scheduling priority for read-only caching; it
does not authorize detection, editing, or unfreezing finalized media.

The stored representation is complete, normalized SRT text. The source codec
is recorded; preserving an editable original ASS/SSA document is a separate
future fidelity improvement before general-purpose subtitle editing.
