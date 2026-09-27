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
  Native container/keyframe boundaries can affect seek precision.
- Video-only works without an audio stream. Subtitle-only displays text and
  offers queued HTML removal where applicable. Downloaded subtitle text opens
  in a separate popup; staged subtitles can also be selected for overlay review.
- Player status stays in the header. Switching does not lock the entire UI.
- HLS.js 1.5.17 and its license are shipped locally, with no runtime CDN request.

## Compatibility and resource limits

Compatible H.264 video and AAC audio are copied. Other audio uses temporary
AAC-LC, 48 kHz, stereo, 192 kbit/s. Incompatible video uses a bounded two-thread
H.264 conversion. Browser MediaCapabilities checks supplement codec metadata;
runtime errors still matter. **Try AAC stereo playback** forces audio conversion
without changing the media. It does not repair an undecodable/damaged source.

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
