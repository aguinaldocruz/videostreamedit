# Matroska layout after media changes

Every container-writing operation checks Tracks placement **after** all header
edits, including IETF language restoration. A successful encoder exit is not a
layout guarantee. FFmpeg followed by `mkvpropedit --set language-ietf=pt-BR`
reproduces late Tracks on a small fixture with no front header headroom.

`matroska_remux.ensure_front_track_headers` is shared by stream edits (including
AAC integration), imports, subtitle HTML cleanup, OCR conversion/finalization,
metadata-only edits and video-title edits. It performs a bounded header-only
check. Good layouts need no rewrite; unknown/malformed layouts are refused.

For late Tracks, a single native mkvmerge packet-passthrough remux is prepared
on the same filesystem under disk-space admission. Tracks, codecs, language and
region, dispositions, titles, chapter times, attachments and meaningful tags
are compared. ISO/IETF normalization is undone, then layout is checked again.
Known invalid-UTF-8 warnings require matching subtitle packet hashes/timing;
other warnings fail closed. No automatic retry/remux loop is used.

Temporary-output callers normalize before publishing their candidate. In-place
metadata edits record the applied metadata before repair, then use the normal
durable replacement receipt; if repair fails the current file is retained and
the workflow fails for review (metadata may already have been applied).
Recovery restoration deliberately bypasses this guard: rollback must restore
the saved bytes, not silently remux the backup.

After publication, a forced layout checkpoint refreshes the report even when
size/mtime were preserved. Checkpoint/database failure is logged, never reported
as a failed media commit. Originals are not deleted until output verification;
temporary repair outputs are removed on normal success/failure. Startup cleanup
recognizes abandoned `.vse-layout-*.mkv` candidates.

Performance: common checks read only EBML element headers and skip payloads.
Actual repair costs one additional read/write pass, only for late Tracks.
Imports already use native Matroska muxing where supported; explicit layout
repair now always uses it to retain header headroom for future edits. Other
FFmpeg workflows can still require this conditional final pass. No catalog
scan or automatic language detection is part of this safeguard.

Regression tests: `scripts/test_matroska_mutation_guard.py`,
`scripts/test_matroska_layout.py`, `scripts/test_matroska_remux.py`,
`scripts/test_movie_import_pipeline.py`. These use disposable fixtures, never
production media. No guarantee is made about future external editor behavior.
