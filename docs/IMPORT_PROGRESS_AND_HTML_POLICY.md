# Media import progress and subtitle HTML policy

The Import Media page handles movies and TV episodes. Select the import type
and destination (the show/season folder for an episode). Plex discovery uses
the destination library of the selected type; episode final-revision preferences
use the same episode note identity as the rest of the application. Internal
`movie_import` job/API identifiers remain shared by both media types.

Optional source removal is part of the server operation for both immediate and
queued imports. It runs after output verification and durability confirmation,
checks the committed destination and original media/sidecars, and returns an
explicit cleanup status and removed-file list. It does not require a second
browser request or send nanosecond fingerprints through JavaScript numbers
(which round those integers and previously caused false source-change failures).
The old standalone browser cleanup endpoint is removed. Failure to clean the
source is reported as an import warning with the reason; it does not repeat the
successful copy. No previously retained originals are deleted retroactively.

Color, italic (`i`, or `em` normalized to `i`), underline (`u`) and line-break (`br`) tags are
not HTML cleanup findings. Cleanup preserves them while removing other presentation tags,
including extra size/style attributes. The same classification is used by
inspection, cleanup, subtitle text previews, and the media-review cleanup offer.
Unknown dialogue markers such as `<John>` are still left alone.
The whitelist includes corresponding closing tags; `br`, `br/`, `br /` and
the subtitle-specific closing form `</br>` are supported case-insensitively.
Break tags do not disturb the surrounding italic/underline/color nesting. Extra
attributes such as size, unrelated styles or event handlers still count as
removable HTML, even on an otherwise permitted tag.

Markup policy version 6 hides outdated HTML findings until they are verified.
Startup updates unchanged non-HTML findings and reclassifies HTML findings from
complete, current database subtitle caches only. It does not extract subtitles,
schedule a catalog run, change damage/language findings, or modify media files.
Uncached/stale findings wait for ordinary subtitle inspection.

Immediate movie import opens the shared modal progress layer as soon as the
user chooses Process whole import now. It remains above the stream editor
through validation, subtitle preparation, movie output, retained external
subtitles, verification, Plex discovery, and optional source removal. File-copy
percentages are read from the hidden destination output size. Bounded copying
checks the free-space reserve throughout the write. Other phases use truthful
step progress; they do not pretend to
know a remux percentage. Short-lived progress records expire after one hour.

Queued imports use the same processing steps in the task progress log. Copying
requires free-space admission, exclusively reserves new outputs, checks for
source changes, and removes only this attempt's partial outputs on failure.
Optional original removal verifies the imported source/subtitle snapshots first.
Plex-refresh failures are reported as warnings after a successful import, not
as a retryable import failure. Saved-value prompts run after the processing
layer is released; importing without any stream edits is supported.

## Single-output import

Immediate and queued imports share `movie_import_pipeline`. Matroska imports
requiring subtitle cleanup/integration, stream removal or stream reordering
write directly from the original inputs to one hidden output in the destination.
All selected changes are consolidated in a single native, stream-copy remux;
video/audio are not re-encoded. Metadata-only Matroska imports keep the faster
copy-and-header-edit path. Non-Matroska imports use a single FFmpeg edit output;
MP4's fast-start finalization may additionally relocate container data.

Valid complete cached text is reused for selected embedded cleanup streams.
Missing selected text streams are extracted together where possible, with
strict decoding and bounded per-track recovery. Color, italics, underline and line breaks remain.
Only retained external subtitles are copied. Integrated or removed sidecars
remain untouched beside the **source** and are omitted beside the imported copy.
Originals are deleted only through the existing explicit, snapshot-verified
source-removal choice, after the imported movie is committed.

Verification checks stream counts/order/codecs, Matroska ISO/IETF language
properties, names, default/forced and accessibility flags, chapters, attachments,
custom tags, and cleaned subtitle text/cue timing. Output language elements are
corrected with small header edits if the muxer normalized them. Verified cleaned
text is saved using the imported subtitle's **output** index, not its original
index. Cache/learning/Plex-discovery failures after commit become warnings.

Destination conflicts are checked before the expensive write and protected
again at publication. Final movie publication uses a same-filesystem no-clobber
hard link followed by removal of the temporary name. Sources are checked again
immediately before publication, including detection of added/deleted sidecars.
The existing disk-admission and free-space reserve cover the whole write.
Queued import does not take an unnecessary full-source workflow backup.
Small ownership receipts under `/data/import-work` allow startup to remove an
interrupted, app-owned hidden output after checking its path/device/inode;
recovery never deletes an original, final movie or unrelated replacement file.
Successful imports retain neither the temporary movie nor its ownership receipt.

The improvement is fewer full-media output passes, not a guaranteed single
read: uncached subtitle preparation and output verification can still scan media.
Tests use disposable real media and count full-output commands; no full-catalog
job or production-media test import is required.

Image-subtitle reports use current indexed codecs and normal report exclusions,
not language-detection matches. Movies and TV-show reports retain graphical
streams even when no text-language result exists. This does not enable OCR.
