# Image subtitle conversion roadmap

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
