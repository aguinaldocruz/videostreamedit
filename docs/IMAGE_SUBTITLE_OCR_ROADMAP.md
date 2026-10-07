# Image subtitle conversion roadmap

## Decision: deferred (2026-10-03)

Image-subtitle OCR conversion is **not approved for routine or automatic use**.
Keep PGS/VobSub streams in their original form, including when cached for
future operations. Do not replace a media subtitle with OCR-generated SRT or
start a catalog-wide conversion. The existing manual conversion/staging code
is retained for future development, not enabled as a scheduled workflow.

A read-only test on *True Grit* (1969), English PGS subtitle 1, produced
1,401 SRT cues with the installed English Tesseract model. The official
`tessdata_best` English model improved some words and punctuation but still
misread standalone `I` as `|`; pipe-character occurrences fell only from
370 to 334. Both test SRTs are under `/data`, while the movie and original
subtitle remain unchanged. Passing SRT syntax and cue counts is not enough to
establish transcription quality. In this VobSub2SRT build, supplying the best
model through `--tesseract-data` crashed; placing it at Tesseract's normal
model path inside a disposable container worked. That workaround is a test
result, not a production configuration change.

Before reconsidering OCR conversion: compare against manually transcribed
sample frames, test direct OCR of native PGS images as well as the VobSub
intermediate, measure errors across languages/fonts, and require explicit
review of the generated SRT before any media replacement. Keep rollback and
source verification mandatory. No OCR model change is deployed to the app.

## Current high-level solution

VideoStreamEdit converts graphical subtitle streams to SRT through format-specific paths. The original media is preserved in `/config/ocr-staging` immediately before remuxing, and the converted media remains reviewable. Approval removes rollback data; rollback restores the original container.

- **VobSub/DVD (`S_VOBSUB`)**: `mkvextract` → `.idx/.sub` → `VobSub2SRT` → `.srt`.
- **PGS/HDMV**: extract and normalize with BDSup2Sub, then OCR rendered subtitle events.
- **DVB/teletext/closed captions**: use the CCExtractor route when supported; unsupported variants fail before remuxing.
- **Unsupported formats**: no unsafe fallback is attempted.

## Language safety

OCR uses Plex-normalized stream language metadata as the authoritative language. A conversion with `und`, empty, unknown, or unsupported language is not started unless a future preflight supplies a detected language with at least 80% confidence. The task records the selected language, confidence, and source in its result.

This prevents a low-confidence language guess from selecting the wrong Tesseract model. It also means an image subtitle with no trustworthy language remains unchanged and is reported for manual correction.

## What the current implementation does

- Queues conversion instead of modifying media during report loading.
- Validates the media path and subtitle stream before conversion.
- Runs format-specific extraction/OCR tools.
- Uses progress steps tied to language validation, OCR, staging, remux, and index refresh.
- Stages a complete original container before remux.
- Preserves other streams, attachments, chapters, metadata, and timing during remux.
- Reindexes the changed media after successful conversion.
- Supports rollback and approval of staged conversions.
- Refuses conversion when language confidence is insufficient.

## What it does not guarantee yet

- OCR is not guaranteed to be perfect; decorative fonts, low resolution, animation, overlapping subtitles, italics, and noisy backgrounds can reduce accuracy. Inspection now flags common corruption signatures such as replacement characters, malformed timing, mojibake, control characters, unreadable payloads, and excessive isolated letters typical of failed OCR. These findings are available in the Damaged SRT subtitles report for movies and TV shows.
- Language preflight for an `und` bitmap subtitle is not yet an automatic multilingual classifier; the current safe fallback accepts only an explicit trusted detected language/confidence supplied by a future detector.
- DVB conversion is not universally equivalent to VobSub. FFmpeg and CCExtractor support depends on the specific DVB variant.
- SRT cannot preserve all bitmap layout, positioning, styling, animation, or multiple-region behavior.
- External image subtitles without a timed container stream are not converted automatically.
- Conversion still requires a full remux; `mkvpropedit` cannot change a bitmap subtitle codec into text SRT.

## Future improvements

1. Add a small multilingual OCR preflight for `und` streams using configured common languages, sampled subtitle frames, Tesseract confidence, and a minimum winner margin.
2. Add per-format conversion capability diagnostics and tool versions to Setup.
3. Add stronger OCR quality checks comparing cue count, text density, confidence, and replacement size.
4. Add optional user review of generated SRT before remux.
5. Keep OCR in an optional worker image so the main application does not need the complete toolchain.
6. Add resumable cleanup for interrupted temporary extraction directories.

## Operational rule

No conversion should replace the media unless the input path is valid, the subtitle stream is identified, the OCR toolchain is available, and language selection meets the confidence rule. Any later mistake must remain recoverable from the staged original until the user explicitly approves cleanup.
