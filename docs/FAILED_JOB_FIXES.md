# Failed-job fixes — October 2026

## Matroska layout repair

- FFmpeg receives an explicit disposition for every stream, including zero,
  so a forced subtitle does not become a default subtitle accidentally.
- Native Matroska remux uses packet passthrough. Layout repair must not decode,
  normalize, repair or transcode subtitle/audio/video payloads.
- Original ISO/IETF language tags, default/forced and accessibility flags,
  chapters, attachments, custom tags and titles remain strictly verified.
- Both stdout and stderr diagnostics appear in failures. Exit code 1 is not
  assumed to mean success: only the known source-encoding warning can pass,
  and only after raw subtitle payload hashes and cue timing match exactly.
  Other warnings, fatal exits, changed metadata and unverified layouts retain
  the original file. Temporary remux outputs are removed on failure.
- Existing disk-reserve checks, timeouts, source signatures, media locks and
  atomic replacement remain in place.

## Subtitle cache

- Embedded SRT tracks are demuxed as raw packets rather than decoded/re-encoded
  by FFmpeg. Multiple tracks still share one extraction pass.
- UTF-8/BOM, UTF-16/BOM and UTF-32/BOM are decoded strictly. Windows-1252 may be
  inferred only for reversible, structurally recognizable SRT without undefined
  bytes, prohibited controls, replacement glyphs or mixed UTF-8/legacy bytes.
  The inference is recorded; it never repairs or rewrites the source file.
- Successfully extracted empty/BOM-only tracks are saved explicitly as
  `empty`, not mistaken for an extraction failure. Complete-media publication
  still requires every expected track and an unchanged source signature.
- Inspection can identify empty tracks and non-UTF-8 embedded source bytes
  even when the cache is readable. Malformed sidecars remain quarantined with
  a clear manual-review/replacement error.
- Existing valid cached subtitles survive the metadata-column upgrade. No
  automatic catalog-wide extraction is started.

## Index history and retries

An older failed index request is retired only when a later successful request
for the same path/index exists, the current file matches the successful index,
and the old workflow has no recoverable artifacts, unsettled LUW or running
stage. The row is cancelled with the successful job reference (normal history
retention still applies); unrelated
unfinished/failed workflow stages are not discarded.

Deployment alone does not automatically retry or delete failed remux/cache
jobs. Invalid source subtitles still need user review, not blind retries.
During the requested follow-up audit, 357 existing cache failures were selected
for one retry on the next scheduled cache run: 211 old empty-output errors,
145 old UTF-8 decoder errors, and one empty external SRT. Representative
extractions succeeded with the updated engine and left source media unchanged.
No full catalog run was started; successful recovery of all retry items is not
claimed until the scheduled worker has processed them.

## Pre-execution safety failures

- Signature and recovery rejection marks the exact workflow stage failed in
  the same transaction as the task row, even before execution starts. The
  workflow cannot remain misleadingly pending after its queue owner fails.
- Only that stage's lock is released. Sibling locks, recovery copies and
  journals remain untouched; terminal stages are never reopened by failure.
- A previous attempt's non-executing, unlocked LUW is also marked failed.
  Retry retains the original enqueue signature: a changed file requires fresh
  validation, not blind replacement of the safety signature.
- Jobs #13531 (Dr. Strangelove) and #13749 (We Bury the Dead) were stale repair
  requests. Both current files were independently checked and their track
  headers already precede the first Cluster. Both requests and their workflow
  projections were cancelled as obsolete without remuxing either file or
  deleting their history. Neither workflow had commit receipts, recovery
  artifacts, active sibling work or resource locks.

## Terminal missing-media outcomes and blocked inspection (October 4)

- Confirmed deleted source files fail permanently with `media_missing`, no
  automatic retry, replacement-path retargeting, Plex-sync request or follow-up
  index cascade. Their unstarted same-media workflow stages also finish failed;
  other media in a bulk operation and all recovery evidence remain untouched.
  A returning file requires a new request, not resurrection of the failed job.
- Permission/I/O failures and unavailable storage anchors are not classified as
  deleted files. Indexing retains bounded retries for these transient outages.
- Retry endpoints exclude permanent missing-media failures, and the task UI
  identifies their outcome instead of offering an ineffective Retry action.
  Scheduled detection drops obsolete catalog requests before creating jobs.
- Stage registration is idempotent under the workflow transaction lock. Exact
  terminal queue owners reconcile phantom unstarted stages. Abandoned LUW plans
  retire only without mutation journals, resource locks or recovery artifacts.
- Windows-1252 mixed-encoding detection requires a complete, canonical UTF-8
  sequence, not an incomplete three/four-byte prefix. Valid `É lá…` legacy text
  is accepted; replacement characters and prohibited controls remain refused.
- Subtitle inspection displays cache failures as blocked, including the cache
  reason and a Review media action. Blocked cache work is excluded from ETA;
  queued history is preserved until a successful cache enables inspection.

Regression scripts: `test_job_outcomes.py`, `test_workflow_terminal_postgres.py`,
`test_subtitle_cache_decode.py`, `test_failed_job_fixes_postgres.py` and
`test_queue_retry_atomic.py`. PostgreSQL fixtures roll back their isolated schema.

Production verification: 13 obsolete-path voice jobs remain in history as
permanent failures, four obsolete deferred requests were removed, and 34
workflow projections/unstarted orphan plans were reconciled. Stargate SG-1
S04E22 was recached in 6.41 seconds without changing the media file; inspection
#69436 completed on its first attempt. Fresh detections #14459–14461 succeeded
for True Grit, Rambo II and MobLand S02E03. Margin Call was not submitted because
it is Final Version. Four genuinely damaged caches remain quarantined; three
associated inspections now show their blocking reason and no invented ETA.
An outdated scheduler test unexpectedly submitted read-only discovery #14457;
it was cancelled before creating any index children, and its mock was corrected.
No full catalog processing run or media repair was performed.

## Queue and cleanup audit (October 6)

- Seven failed editor tasks repeated five successful edits. Repeated clicks
  had queued the same proposal against the same source, then the first task
  legitimately changed that source. The signature rejection was correct;
  retrying those failed duplicates would not restore useful work. Immediate
  waiting/submit guards and transactional backend deduplication now prevent
  creating such duplicate editor tasks.
- Of 131 pending subtitle inspections, 40 scheduled detection requests had no
  subtitle or external track. They can finish an empty inspection without a
  cache; fresh manifests still detect newly arrived sidecars. Future scheduled
  requests exclude known no-subtitle media. The other 91 were genuinely waiting
  for missing/partial caches after edits or initial indexing, not running in a
  completion/requeue loop. The UI distinguishes Waiting for cache from blocked
  cache failures. Cache schedule and user Run now remain unchanged.
- Seven core-index failures referenced absent Below Deck Mediterranean S11
  E11–E17 files, also absent from the catalog. They remain permanent missing-media
  outcomes rather than automatic retries against invented replacement paths.
- HTML false refusals included FFprobe cover art exposed as attached video,
  cached-vs-container timestamp origins and normalized markup in untouched
  older caches. Native track mapping now distinguishes attachments, replacement
  cue timestamps use the verified container/native origin, and untouched native
  text can be accepted only with exact packet and timing evidence. Unverified
  old untouched caches are discarded for later recaching, not rebound blindly.
  AAC CodecDelay headers are preserved and verified explicitly, including
  header-layout repair; a missing decoder-delay header is not accepted as an
  unexplained audio-only timeline change.
- Per-track extraction provenance keeps existing caches usable for reading.
  Only obsolete selected mutation inputs are freshly extracted with strict
  decoding before cleanup/import/autofix. This does not invalidate or rerun the
  full catalog. Autofix consent digests remain mandatory; an outdated preview
  must be reviewed again, not silently replaced with different approved text.
- Genuine prohibited controls, undecodable text and malformed cues still
  refuse mutation and retain the original media. Those cases need manual review
  or a fresh supervised replacement, not weaker validation.

Regression checks include real negative-start AAC/Matroska fixtures, PNG cover
attachments, legacy selected/untouched caches, exact dialogue/cue comparison,
isolated PostgreSQL deduplication and browser queue success/rejection/double
click/navigation/draft tests. Browser writes are intercepted; database fixtures
roll back their isolated schemas.

Production verification after deployment: queue double-click/rejection/draft
tests and read-only report navigation passed against the deployed assets, with
all browser mutations intercepted. Health/readiness and the operational smoke
checks passed. Yentl #15123 replaced three subtitles in one verified remux and
published its final cached subtitles. Stripes #14759 also completed
successfully using its valid cached subtitle,
with no extraction, one remux and strict audio/video/subtitle timing checks.
Twenty-two unchanged HTML requests and
the existing approved autofix #15135 were resubmitted through the normal retry
API, without replacing signatures or consent. #14879 was not retried because
its original source signature no longer matches. Damaged inputs and historical
duplicate editor jobs were left for review. No full catalog run was started;
task and index pause controls were restored to their original running state.
