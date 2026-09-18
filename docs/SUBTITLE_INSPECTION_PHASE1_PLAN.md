# Subtitle inspection — approved goal and Phase 1 execution plan

Status: approved for implementation  
Approved: 2026-09-18  
Current phase: Phase 1b (foundation reports and controlled revalidation)  
Data policy: subtitle inspection results and transient detector state may be recreated from scratch. Durable configuration and learned values are preserved.

## Goal

Provide a fast, incremental and explainable subtitle-inspection system that helps manage, report and analyze media streams without triggering unrelated work or blocking interactive editing.

## Objectives

- Detect subtitle metadata mismatches only when evidence is reliable.
- Distinguish mismatch, no-confidence, unreadable, unsupported, skipped and healthy results.
- Explain why a subtitle was flagged.
- Reuse one text extraction for health, HTML, damage, language and SDH analysis.
- Recheck only the affected subtitle stream after a media change.
- Keep voice detection independent from subtitle-only changes.
- Expose detector health, versions, queue state and last successful checks.
- Preserve current reports, filters, navigation and review workflows.

## Phase 1 implemented

- Shared per-media subtitle text cache between extended subtitle indexing and local language/SDH inspection.
- Stream-level fingerprints containing media state, stream identity, codec, metadata, external-file state, configured languages and detector version.
- Persistent `subtitle_detection_stream_state` records with status, reason, signature and check time.
- Explicit analysis fields on `portuguese_language_detection`: status, reason, detector version, cue count, text size/coverage, markup count and damage classification.
- Detector version advanced to v10 so existing subtitle analysis is safely refreshed when queued.
- Targeted invalidation clears only the selected subtitle stream state; full invalidation clears all subtitle detector state for that media.
- Orphan cleanup removes detector state for media no longer in the Plex catalog.
- Read-only `/api/v79/language-detection/health` endpoint for Setup and diagnostics.
- Setup health summary showing detector version, stream statuses, queue backlog and last checked time.
- Existing mismatch reports continue to show only confident mismatches; no-confidence markers use explicit statuses and skipped non-common languages do not create false warnings.
- No-confidence/unreadable subtitle reports are available for movies and TV episodes, with per-stream reason and evidence metrics.
- Report actions can open the normal stream editor or queue targeted revalidation for one or more listed subtitle streams.
- Setup health now reports detector version, stream status totals, subtitle queue state and a duration-based ETA when samples exist.
- Targeted revalidation uses the existing subtitle queue and does not synchronously inspect media.
- Codec labels are normalized (for example, `SubRip/SRT`) so text subtitles are not mistaken for graphical streams.
- Health, smoke and functional approval checks pass after deployment.
- Read-only regression gate validates targeted invalidation, operation scopes and fingerprint invalidation (`scripts/subtitle_inspection_regression.py`).
- Quality findings now override language matches for malformed/OCR-gibberish and advertising/link-like subtitle payloads, keeping them actionable as no-confidence results.
- Balanced Portuguese/English evidence is reported explicitly as mixed-language no-confidence evidence instead of forcing a regional language.
- Repeated vocabulary and regional patterns are capped per marker so songs, credits, and OCR repetition cannot inflate confidence.
- Offline Portuguese spelling markers from the curated lexicon now contribute bounded PT-BR/PT-PT evidence.
- Sparse subtitle evidence is confidence-capped, preventing one or two cues from creating a strong mismatch warning.
- Health now exposes stale result counts after detector upgrades, without scheduling automatic work.
- Diagnostic reports now include a bounded normalized evidence excerpt; this is not a subtitle preview cache and is populated only when a stream is inspected.
- Evaluate now carries language confidence calibration, quality issues, and bounded evidence into forced/full-subtitle recommendations; damaged or promotional text is sent to review first.
- The Subtitle inspection index tab now surfaces stale media alongside running/queued/failed/indexed counts, without starting work automatically.
- Index maintenance controls are grouped visually into Update, Maintenance, and Runtime actions while retaining the existing queue endpoints.
- Recovery controls (retry/delete failed, pause/stop) and finished-history cleanup are now grouped separately from normal indexing actions.

### Foundation hardening — complete

- Detector version is centralized in an import-cycle-free configuration module.
- The read-only subtitle inspection gate verifies health, report filtering and an idle queue without scheduling work.
- Bounded real-world fixtures cover text, graphical, sparse, SDH, mixed-language, mojibake, OCR and advertising/link cases.

### Phase 2 — analysis quality — complete

- Bounded normalized-text evidence samples are available for diagnostics without a preview cache.
- Mixed-language, advertising/link, OCR-gibberish, mojibake and malformed-subtitle classification is explicit.
- Portuguese-Brazilian versus Portuguese-Portugal evidence uses bounded vocabulary, spelling and pattern markers.
- Forced-subtitle evaluation combines coverage, cue density, SDH, language and quality evidence.
- Sparse evidence is confidence-capped and stale detector results are visible without automatic full rebuilds.

### Phase 3 — interface consolidation

- Keep common-language and detector configuration in Language Detection.
- Move generic queue/scheduling controls to Tasks/Indexes.
- Move duplicate-language configuration to Reports.
- Move OCR staging to Media Operations/Maintenance.
- Replace ambiguous full-detection actions with separate subtitle and voice actions.
- Keep advanced sample-position controls behind an Advanced section.

### Phase 4 — measured optimization — baseline complete

- Bounded extraction benchmark completed across SubRip/SRT, HDMV PGS and VobSub.
- No additional cache or aggressive parallelism is justified by current measurements.
- Retain no full subtitle-preview cache unless a later user-approved benchmark justifies it.

## Safety and acceptance criteria

- No track-name, default/forced or unrelated video change starts language or voice detection.
- Subtitle language/region changes recheck only the affected subtitle stream.
- Subtitle content, integration, removal or stream-identity changes recheck affected subtitle streams.
- Audio changes trigger voice detection only for affected audio streams.
- A failed subtitle stream does not stop other media from processing.
- Reports never treat skipped non-common languages as no-confidence findings.
- Existing filters, reports, navigation boundaries, immediate edits and queued edits remain available.
- All changes remain recoverable through the existing queue/LUW workflow.

No destructive migration is required for this phase. Existing detector rows can be regenerated by queueing subtitle inspection; configuration and learned data remain untouched.
