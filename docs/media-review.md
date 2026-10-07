# Media Review and supervised AAC audio

Media Review is an on-demand player, not a persistent preview cache. No catalog
conversion or full-catalog run is started by this feature.

## Watching and switching

- Audio / Video / Subtitle toggles retain at least one selected output.
- Audio selection restarts a **short playback buffer at the current position**,
  retaining volume, speed and play/pause intent. It may briefly buffer; it is not
  a promise of sample-perfect gapless audio switching.
- Subtitle selection replaces the WebVTT overlay without rebuilding playback.
  Text is reused only during the current review session. Image subtitles are
  explicitly unsupported, never silently burned into video.
- The full-media slider and ±10-second controls seek within the current browser
  buffer when possible; other positions request a new short server buffer.
  A preceding keyframe is copied for decoding, then skipped in the browser to
  reach the requested position without shifting subtitle timing.
- Video-only works without an audio stream. Subtitle-only displays text and
  offers queued HTML removal where applicable. Downloaded subtitle text opens
  in a separate popup; staged subtitles can also be selected for overlay review.
- Player status stays in the header. Switching does not lock the entire UI.
- HLS.js 1.5.17 and its license are shipped locally, with no runtime CDN request.

## Compatibility and resource limits

Compatible H.264 video and verified AAC-LC mono/stereo at 44.1/48 kHz are copied.
Unknown AAC profiles, surround audio and other codecs use temporary
AAC-LC, 48 kHz, stereo, 192 kbit/s. Incompatible video uses a bounded two-thread
H.264 conversion. Browser MediaCapabilities checks supplement codec metadata;
runtime errors still matter. **Try AAC stereo playback** forces audio conversion
without changing the media. It does not repair an undecodable/damaged source.

### Synchronization and playback reliability

FFmpeg preserves the original common video/audio timestamps (`copyts`), including
seek preroll. The player maps HLS.js's measured timestamp offset back to the
source's timestamp origin; video, audio, subtitle cues and the timeline slider
share that clock. The requested seek time is never used as a guessed subtitle
offset. The inner transport muxer also preserves timestamps rather than adding
its own negative-timestamp shift. See [FFmpeg's timestamp options](https://ffmpeg.org/ffmpeg.html#Advanced-options).

Video playback uses timestamp-preserving transport segments. Audio-only uses
fragmented MP4 to avoid audio packet discontinuities at transport/PES boundaries;
the first fragment's edit-list offset is measured before playback becomes ready.
Both paths retain the original movie position when changing playback mode.

AAC-LC is explicitly declared to HLS.js for both copied and converted audio;
the browser is not left to guess AAC-LC versus HE-AAC. Subtitle tracks are hidden
until their cue times are mapped, and obsolete tracks are disabled before removal.
Generation checks isolate delayed callbacks from earlier stream selections.
Changing playback releases the previous decoder and server producer immediately.
An overlay failure is reported and pauses review, rather than silently presenting
a subtitle-less video as a successful synchronized review.

Read-ahead is measured from completed HLS segments and source-time heartbeats,
not FFmpeg's progress clock, which can rebase after seeking. A small bounded
timestamp probe reads only the first completed temporary segment. No full-media
conversion, additional catalog scan or permanent media change is required.

`MEDIA_REVIEW_WORKERS` defaults to 2 (allowed 1–3). At most six active/waiting
sessions exist. FFmpeg runs at lower CPU priority with bounded read-ahead,
pauses when more than 45 seconds ahead, and deletes old HLS segments. Each
session is stopped at 512 MiB of temporary files or when free space falls below
512 MiB. Closing, changing the playback session, or 90 seconds without a
heartbeat cancels work; startup removes abandoned UUID-named playback buffers.
Run one application worker: interactive session ownership is process-local.

## Permanent AAC version: explicit approval

1. Select original audio, then **Create AAC stereo version**. A normal task
   prepares only that audio; the media file remains unchanged.
2. The compatibility section shows queued/running/failed/ready status. Ready
   audio appears in the audio selector for synchronized comparison.
3. **Approve: add track** retains the original audio and adds a non-default,
   non-forced AAC stereo track. **Replace original** explicitly warns about
   losing the original codec and surround channels.
4. Outside TV edit mode, approval queues a protected `media_edit` task. Within
   an open TV draft, approval is journaled and applied with that episode's
   consolidated edit at Save; Discard returns the audio to awaiting approval.
   Approved draft audio remains visible in the review selector and stage list.
5. Rejection removes the staged audio. Approved audio and records are marked
   complete and its files removed after successful integration.

Permanent integration currently supports Matroska containers only. Other
containers still support temporary review conversion. Final Version locks are
respected; approval does **not** automatically mark a media as Final Version.

Staging is `/data/review-audio` (override `MEDIA_REVIEW_AUDIO_STAGE`). Only audio
is retained for review. Final integration necessarily writes a complete sibling
container while copying video, subtitles, attachments and other unchanged
streams. It does **not** keep another full-media rollback snapshot: the original
is untouched until verification and atomic replacement. A durable commit intent
records the temporary path and expected output fingerprint for startup recovery.
Source changes invalidate approval. Free space is checked before and throughout
conversion/remux. Original media is retained on a pre-commit failure. Audio
changes invalidate audio detection, not unrelated subtitle language findings.

## Tests

`scripts/review_test_server.py` runs in a disposable container without production
mounts/credentials. `scripts/test_review_player.py` exercises real browser HLS,
subtitle switching, audio switching, rapid seeks, video-only playback, layout,
real AAC staging and add/replace approval, stale-source rejection and cleanup.
The fixture contains generated H.264 video, AAC and AC3 audio, and a text subtitle.
It includes B-frames, six-second keyframe spacing and a nonzero source origin.
`scripts/test_review_sync.py` checks active subtitle cues after non-keyframe seeks,
copied/transcoded audio changes, decoded 440/660 Hz browser tones, audio-only
playback, rapid cancellation, stale-track disabling and
producer cleanup. `scripts/test_review_timeline.py` verifies nine real FFmpeg
copy combinations, encoded AAC payloads, bounded priming-frame differences and
continuous timestamps. Its optional `--media FILE` checks a short read-only sample
with each audio track; it never modifies the selected file. Browser integration
currently covers Chromium/HLS.js; native-HLS Safari still needs device validation.
