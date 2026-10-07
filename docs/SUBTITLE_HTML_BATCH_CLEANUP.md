# Batch subtitle HTML cleanup

Individual cleanup, report bulk cleanup and queued editor HTML cleanup use
the same media-scoped engine. One movie or episode is the commit boundary;
a TV show/report request still creates independent media tasks.

## Processing

1. Validate all selected stream/sidecar identities, deduplicate targets and
   read all checksum-verified cached text in one database transaction. Image
   tracks cannot be selected for HTML cleanup. Final Revision edit protection
   and queued-source signature checks remain in effect.
2. Extract missing embedded text together, before any original is changed.
   A failed extraction batch can be retried per track for diagnosis; invalid
   text is never silently accepted. Already-clean targets need no mutation.
3. Prepare every changed subtitle. Color, italic/emphasis, underline and
   line-break tags (and their closing forms) remain whitelisted. SRT sidecars
   preserve their encoding and BOM; ASS/SSA/VTT sidecars remain native files,
   not SRT text written with a different extension.
4. For embedded changes, build one sibling media output containing every
   replacement. Matroska uses the native packet-passthrough writer; metadata
   is written into the initial headers, avoiding preventable header relocation
   and a second layout-repair pass. Non-Matroska uses explicit stream-copy
   mapping; only changed subtitle streams are encoded when the container needs
   it. Audio/video are not re-encoded.
5. Verify readable stream count/order, codecs, subtitle cue count/text/timing,
   default/forced flags and language metadata. Matroska additionally verifies
   region tags, other accessibility flags, track/global tags, chapters,
   attachments and container title. Untouched cached embedded texts join the
   same verification extraction pass. The final header-layout guard remains
   mandatory. Source media and sidecar stamps are rechecked before commit.
   The known source-encoding warning is accepted only with byte-exact packet
   and timing evidence for every untouched subtitle; deliberately replaced
   texts still require full cue verification. Any other warning is refused.
6. Atomically replace each prepared output with fsync and durable commit
   receipts. Publish changed and retained text caches together in one database
   transaction. An untouched complete image cache can be rebound only when
   its source signature and unchanged track indexes/codecs match. Cached binary
   payloads are not transferred or rewritten. Partial/stale image caches are
   invalidated and recached instead.
7. Queue a core metadata refresh followed by one subtitle-inspection refresh.
   The index keeps complete or partial caches already published for the current
   source; only stale portions are invalidated. Presentation cleanup does not
   request voice or subtitle language detection. Only genuinely missing cache tracks become priority
   recache work; a complete retained cache clears the captured pending revision
   without discarding a newer change request.

## Safety and recovery

The original container remains in place until its replacement passes all
checks. Disk admission and running-write reserve checks still apply. There is
no extra full-media workflow backup for this verified, idempotent operation:
one sibling output is sufficient, as for Matroska layout repair. Existing
failed-workflow recovery files are not deleted by this change.

Small sidecars are fully prepared before committing any file. Filesystem
replacement is atomic per file, not a cross-file database transaction; durable
file receipts and idempotent HTML removal allow an interrupted media/sidecar
request to finish safely without repeating a completed remux. A media changed
externally after commit is refused by recovery checks. Old per-track HTML
receipts resume only their uncompleted targets, then use one batch.

If cache publication fails after verified media replacement, the cache is
invalidated and priority recaching is requested. The successful filesystem
change is not misreported as a retryable remux failure. An index/bookkeeping
failure after commit uses its HTML receipt on retry instead of remuxing again.

The same queued task owns its deterministic temporary filename; retry removes
an uncommitted leftover before space admission. Normal failures clean their
owned output immediately; startup recovery also recognizes these sibling
artifacts. No full-catalog scan is started by this implementation.

## Verification

- `scripts/test_subtitle_cleanup_batch.py`: real native remux, multiple cached
  tracks, one missing-track batch, untouched-cache retention, partial cache,
  interleaved streams, exact pt-BR/pt-PT and metadata, mixed external BOM/legacy
  SRT and native VTT, known/unknown warnings, post-edit index retention, no-op,
  invalid targets, disk/verification failures, source races and cache outages.
- `scripts/test_subtitle_cache_foundation.py`: atomic complete/partial cache
  publication, image rebinding without payload copies, SQL failure rollback,
  corrupt/stale data rejection and pending-revision safety.
- Existing import, HTML-whitelist, header-layout and durable job-recovery
  regression suites cover the shared writer and retry boundary.

Task results/logs include target and changed counts, remux count, cached/missing
input counts, total elapsed time, remux time and output-verification time.
Queue progress identifies preparation, remux, verification, cache publication
and index follow-up separately; a remux without tool percentages shows its
current step rather than fabricated percentage progress.

## Timestamp and legacy-cache verification

Cached SRT exports and native Matroska packets can have different timeline
origins. Replacement inputs are offset explicitly using the source start and
the native writer's recorded negative-timestamp shift; CodecDelay alone is not
evidence of a packet shift. Verification subtracts the output origin and proves
the same global shift on untouched audio/video. Cue dialogue, durations and
relative synchronization remain exact, rather than accepted within a widened
timing tolerance. A replacement with a negative first cue is refused before
writing because the writer could otherwise drop it.

CodecDelay is also part of verified native metadata. Some AAC inputs have
nonnegative physical blocks plus a positive decoder-delay header that mkvmerge
does not retain automatically. The copied output restores that exact header
before timeline/layout verification; merely accepting a changed first audio
timestamp is not allowed. Explicitly transcoded compatibility audio uses the
new input's own delay, rather than inheriting another codec's delay.

An old untouched cache that differs only from the preserved source representation
can be bypassed only after byte-exact packet/timestamp verification; that cache
is dropped, not published as verified. Selected replacement texts never use this
fallback. Old selected cache revisions are refreshed from strict raw source
extraction first, in one batch. New verified outputs record extraction revision
2. Existing readable caches are not invalidated catalog-wide.

## Production verification (2026-10-06)

Deployment waited for active job #14748 to succeed, then resumed the existing
queue. Jobs #14749 and #14750 completed using cached inputs without original
subtitle extraction. Job #14749 retained both cached subtitles after changing
one, including after its core and subtitle indexes completed. Both media had
no priority recache request and no workflow backup artifact; no voice detection
jobs were created. Their cleanup-engine times were 22.49 s and 44.01 s (29 s
and 52 s for the complete queue handlers). These are different media and must
not be treated as controlled before/after speed measurements.

Job #14752 (Rain Man) cleaned two cached embedded subtitles with **one remux**,
zero source subtitle extractions and no full-media backup artifact. The cleanup
engine took 74.25 s, including 41.54 s for writing/header checks and 20.55 s for
output verification; its full queue handler completed in 87 s. Both cached
tracks remained complete after successful core/subtitle indexing, with no
priority recache request.
