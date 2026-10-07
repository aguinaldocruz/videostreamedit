# Reviewing suspected subtitle damage

Movie and TV **Damaged SRT subtitles** reports include **Reasons & examples**.

- Reasons: up to 30, ranked by the number of affected subtitle streams, with distinct media counts. One stream can have multiple reasons.
- Expand a reason to read existing valid subtitle caches. This does not extract media, run inspection, queue work, or modify detection rules.
- Examples: up to 30 distinct patterns, ranked by occurrences, with stream and media counts. Encoding and empty-track examples count once per stream.
- Expand an example for up to three distinct stream locations, cached-text line/cue/timing, a short excerpt, and a Stream properties shortcut within the report's navigation boundaries.
- **Autofix** beside Stream properties offers a configured language rule, then
  full corrected-text approval/rejection for every matching SRT/SubRip stream
  with changes in that movie/episode. After all decisions, choose immediate or
  queued replacement; approved embedded streams use one verified remux. See
  [Subtitle autofix rules](SUBTITLE_AUTOFIX_RULES.md) for scope and safeguards.
- Missing/unusable caches and findings not reproduced from cached text are reported explicitly. Rankings cover available cached evidence, not unavailable streams.
- Counts describe the whole report, not its client-side search or visible page. Reload the report for updated results.

These are clues, not proof of damage. Normal Unicode letters and punctuation in the common Setup languages (and other languages) are allowed without relying on potentially incorrect track metadata. The mojibake rule requires byte-compatible broken UTF-8 sequences, so **ÃO**, **Ângela**, curly quotes, dashes, and ellipses do not trigger it. Genuine sequences such as **Ã£** and **â€™**, replacement characters, and hidden control codes remain detectable. Punctuation-only text may lack language evidence but is not automatically damaged.

Legacy source encoding does not prove that decoded subtitles are unreadable. Each reason includes its own caution. No subtitle text is rewritten by these checks.

## Supervised UTF-8 encoding quickfix

**UTF-8 quickfix** appears beside affected movies/episodes and in the
**Non-UTF-8 source bytes** evidence section. No saved autofix rule is required.
Prepare a full-text preview, approve/reject every eligible stream, then choose
**Apply now** or **Queue replacement**. Preparation never changes media.

This narrowly scoped operation supports existing SRT/SubRip streams with
recorded, strict and reversible **Windows-1252 (inferred)** decoding. It verifies
the legacy round trip again, validates all cues, and refuses mixed encodings,
replacement glyphs, controls, empty/malformed streams and possible mojibake.
It does not guess another encoding or reconstruct a missing character. Refused
streams remain unchanged with a specific explanation.

The original and UTF-8 panes intentionally show the same decoded words and
timing: the fix changes bytes, not dialogue. All approved embedded streams in
one media share a single metadata-preserving, verified remux; approved external
SRT uses an atomic UTF-8 write. The source signature, original text digest and
encoding evidence are checked again at execution. The verified cache is
updated to UTF-8 and targeted inspection determines whether the warning remains.
It uses the same consent, bounded review, busy progress, media lock, disk guard,
queued/immediate execution and retry receipts as supervised Autofix. No
full-media recovery copy is retained after successful completion.

API preparation: `POST /api/subtitle-autofix/encoding/prepare` with `path` and
`operation_id`; subsequent review/decision/apply endpoints are shared with Autofix.

## Bulk Windows-1252 UTF-8 quickfix

In either movie or TV reports, expand **Non-UTF-8 source bytes**, then
**Windows-1252 (inferred)**. **Queue all UTF-8 quickfixes (N media)** submits
the entire current encoding group, not only its three example locations or
the report's visible page. The movie and TV scopes stay separate. This is an
explicit trusted-user action **without individual previews or approvals**;
the individual supervised quickfix remains available and unchanged.

Submission creates one lightweight background preflight request, immediately
shows waiting feedback and blocks repeat clicks. Validation creates at most
one normal **Subtitle autofix** task per eligible movie/episode. Pending media
are hidden from reports, and results are available in the usual task/preflight
details. Final Version media, active TV drafts and pending media changes remain
protected. There is no automatic full-catalog repair or inspection run.

Only listed embedded SRT/SubRip tracks with ready, complete Windows-1252 cache
evidence are selected. The worker reuses the individual quickfix's full strict
round-trip, cue, timing and corruption checks, using a signature/hash-validated
cache or refreshing just selected older mutation inputs. Already-UTF-8 tracks,
other encodings/codecs and unlisted streams are not rewritten. Mixed encoding,
mojibake, controls and malformed input are refused with explanations; if no
selected track is safe, the task completes without changing the original.
External changes refuse the outdated request rather than weakening its checks.

All valid selected streams of one media share **one verified remux**, retaining
words, timing, formatting, native language/region, titles and stream flags.
Exact validated texts are saved with the running job before any media write;
existing media locks, disk guards, atomic commit receipts and retry recovery
remain in force. The resulting cache is updated to UTF-8 and only affected
subtitle findings are reinspected. Successful work retains no full-media
rollback copy. No data migration or report rebuild is needed for this action.

API: `POST /api/subtitle-autofix/encoding/queue-report` with
`{"kind":"movies"}` or `{"kind":"tv"}`. The response includes the preflight
request ID and selected media/subtitle counts, not corrected full texts.

Regression checks: `scripts/test_subtitle_encoding_bulk.py`,
`scripts/test_subtitle_autofix_workflow_postgres.py`, and
`scripts/test_subtitle_damage_report_ui.py` cover full-group scope, repeat
submission, unsafe input, durable replacements, restart safety and both UIs.

## Replacement-character dictionaries

`scripts/export_subtitle_replacement_words.py` exports **all** affected streams
in the current movie and/or TV reports, not only the top 30 examples. It reads
complete, source-signature/hash-validated cached subtitles first. Only missing
cache tracks need a bounded complete diagnostic extraction; partial failures
are never silently accepted. A source changed during reading is reported and
its results discarded. No media, cache, report, rule or queue is modified.

```sh
python scripts/export_subtitle_replacement_words.py \
  --kind all --output /data/subtitle-replacement-dictionaries/new-review
```

The output directory must not already exist, protecting manually edited files.
Each language/region gets a UTF-8 `.txt` containing one unique whole word with
`�` (U+FFFD) per line. Current native stream metadata determines the language;
`pt`, `pt-BR`, and `pt-PT` stay separate, and unknown metadata uses `und`.
Case, accents and internal apostrophes/hyphens are retained; formatting tags,
links and surrounding punctuation are omitted. A standalone `�` is kept since
the whole word may be missing. Valid Unicode punctuation is not corruption.

`manifest.json` records scope, coverage, counts and output filenames.
`references.jsonl` links every word/count to its source media, stream, language,
encoding and full-text hash, including unreproduced findings and failures.
Any glyph introduced by diagnostic decoding is explicitly marked there.
The dictionaries are for manual review only: no replacement is inferred or
automatically installed. Future user-authored From → To rules can use them.

Regression checks: `scripts/test_subtitle_encoding.py`,
`scripts/test_subtitle_replacement_export.py`,
`scripts/test_subtitle_autofix_media.py`,
`scripts/test_subtitle_cleanup_batch.py` and both damage-report / autofix-review
browser tests cover normalization, complete export and supervised UI behavior.

## Isolated-letter / OCR findings

This check analyzes visible dialogue, not HTML tag names, attributes, ASS style
commands, URLs, cue numbers or SRT timing. Numeric notation such as `43m32s` and
`1h07m47s` is not split into isolated `m`, `s` and `h` letters. Unicode entities
are interpreted and decomposed accents are normalized in the analysis copy only;
the original/cached subtitle and its styling remain unchanged.

An OCR finding requires at least 12 words and eight single-letter words, plus
either a single-letter ratio of at least 35% with eight letters in fragmented
runs, or symbol-rich corruption (a ratio of at least 30%, at least eight unusual
isolated letters and 25% punctuation/symbols in the visible payload). The second form
keeps genuinely garbled OCR with punctuation between letters detectable. A
run has at least three whitespace-separated single letters,
including two beyond common one-letter grammatical words. Dotted initials,
hyphenated spelling and ordinary Portuguese words such as `a`, `e`, `o`, `é`
do not provide fragmented-word evidence by themselves. Deliberate spaced-out
spelling may still resemble broken OCR: the label is a review hint, not proof
that OCR was used.
Contractions are counted as words; uncased one-character words such as
ideographs are not presumed to be fragmented alphabetic OCR.

The examples list uses the same visible-text/fragment rules, so italic lyrics
and normal short sentences no longer appear as OCR evidence merely because
their words or markup are short.

To refresh **existing** isolated-letter warnings from complete caches:

```sh
python scripts/revalidate_subtitle_damage.py --ocr-only           # dry run
python scripts/revalidate_subtitle_damage.py --ocr-only --apply   # update findings
```

This bounded refresh covers affected movie and episode tracks, preserves other
damage reasons, verifies cache hashes, and guards against concurrent cache or
inspection changes. It does not extract subtitles, modify media, queue a
catalog run or discard unavailable evidence; uncached/pending/failed items wait
for their usual complete-cache inspection.

Regression checks: `python scripts/test_subtitle_damage_report.py`,
`scripts/test_subtitle_damage_report_ui.py` (Playwright), and
`scripts/test_subtitle_damage_revalidation_postgres.py` (configured PostgreSQL;
creates/removes a disposable test schema, never edits production catalog rows).
