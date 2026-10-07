# Subtitle autofix rules

## Current scope

Manage rules in **Setup → Editing → Subtitle autofix**. This phase provides
durable rule configuration, a sample preview, and supervised repairs from the
movie and TV **Damaged SRT subtitles** reports. Saving, enabling, or
sample-previewing a rule does not modify media, cached subtitles, reports, or queues.
There are no default substitutions: corrections must be deliberately entered
and checked by the user.

Each rule has a name, optional description, enabled/disabled state, 1–3
subtitle metadata languages, and an editable ordered list of From → To values.
Replacement rows can be added, removed, or moved up/down. Up to 500 rows are
supported per rule. Disabling a rule preserves its configuration.

## Matching and preview

- Language choices follow the project's saved language/region order, with
  offline fallback choices. ISO aliases such as `por` and `eng` are normalized
  to `pt` and `en`. Regionless `pt` covers Portuguese in any region;
  `pt-BR` and `pt-PT` select those specific variants. Unknown/empty metadata is
  not guessed; the explicit Undetermined option matches it.
- Replacements are literal and case-sensitive. “Anywhere” replaces character
  sequences; “Whole word / phrase” also requires Unicode word boundaries.
  Regex symbols and replacement backslashes have no special meaning.
- From cannot be empty. Empty To deletes matched text. Whitespace and Unicode
  are preserved exactly; line breaks cannot be entered in replacement rows.
- Rows execute top to bottom. Later rows may affect an earlier row's output.
  The preview shows per-row and total replacement counts for reviewing this.
- Preview accepts up to 32,768 characters of dialogue, not complete subtitle
  structure. It works on unsaved or disabled drafts without saving them. Output
  expansion is bounded to prevent accidental memory-intensive rules.

Save is explicit. A saved rule no longer appears as pending; unsaved drafts
survive navigating between Setup sections. Closing or replacing an unsaved
draft asks for confirmation. Errors remain visible and do not discard edits.

## Persistence and safety

Rules are stored individually in `application_settings` under
`subtitle-autofix-rule/<id>`, with a schema version, revision, and timestamps.
Normal full-system PostgreSQL backups therefore already contain them. No data
migration or catalog rebuild is needed. Reads load small configuration records
only, lazily when this Setup section is opened.

Update/delete requests require the rule's revision and use an atomic
compare-and-swap in PostgreSQL. Concurrent/stale screens cannot silently
overwrite or delete newer rules. Unreadable saved records are reported and
retained, never silently dropped. Logs contain rule IDs/counts, not replacement
text.

API: `GET/POST /api/settings/subtitle-autofix/rules`,
`PUT/DELETE /api/settings/subtitle-autofix/rules/{id}` (delete also requires
`?revision=N`), and `POST /api/settings/subtitle-autofix/preview`.

## Supervised media repair

1. Open either Damaged SRT subtitles report and click **Autofix** beside
   **Stream properties**, on a movie or episode. The same shortcut appears
   beside stream locations in Reasons & examples.
2. Choose one enabled rule matching current subtitle metadata. **Prepare
   previews now** immediately reads valid cached text, or batch-extracts missing
   embedded text. Preview preparation is never queued and never replaces media.
3. Read the complete corrected SRT in a popup, optionally displaying the
   original beside it. **Approve this subtitle** or **Reject this subtitle**
   advances through every matching stream with effective changes, not only
   streams flagged as damaged. Unchanged streams need no approval; unreadable
   or unsupported streams are left alone with an explanation.
4. Only after all individual decisions, choose **Apply now** or **Queue
   replacement**. All approved embedded streams in that movie/episode share
   one verified remux. Approved external SRT sidecars are replaced at their
   existing location without adding/removing a track. Rejected streams remain
   unchanged. With no approvals, nothing is submitted.

Current media support is **SRT/SubRip only**, embedded or external. Image
subtitles and other text formats are not implicitly OCRed or converted. Rules
affect dialogue, not cue numbers, timestamps, existing HTML presentation tags,
or ASS override tags. Cue counts and timings must remain identical. A rule is
selected explicitly; multiple rules are never combined automatically.

## Execution safeguards and cache behavior

- Preview source configuration is checked before and after preparation, again
  at submission, and under the execution workflow's media lock. Original text
  digests are checked too. A changed source is refused rather than applying an
  approval to a different subtitle. Native Matroska IETF languages distinguish
  `pt`, `pt-BR`, and `pt-PT` without silently guessing a region.
- Final Version media, media with pending edits, and real files involved in an
  open TV-show draft cannot be changed by this report action.
- Previews expire after one hour of inactivity and use bounded, temporary
  memory: 32 MiB per subtitle, 64 MiB across review texts, and 16 open reviews.
  Closing/rejecting creates no replacement job. Restarting before submission
  expires previews; restarting after submission preserves the approved task.
- Approved text, not a later reapplication of a possibly edited rule, is saved
  durably with one media-scoped task. Double submission returns the same task.
  Full subtitle text is omitted from routine queue listings/details.
- Apply now uses the existing safe task executor with immediate priority and
  step-by-step waiting progress. It may wait for an already-running safe media
  operation; it does not interrupt another remux. Queue returns immediately.
  A paused executor preserves approvals and offers queueing instead.
- Replacements use the existing cache-first remux writer, disk admission,
  atomic replacement receipts, and output verification. Stream order,
  languages/regions, titles, flags, tags, chapters, attachments and unrelated
  subtitles are retained. External SRT is written in UTF-8, preserving an
  existing UTF-8 BOM. Successful work leaves no extra full-media rollback copy.
- A restart/retry uses exact committed-output receipts and never repeats a
  remux already applied. Failures before replacement retain originals; failed
  task details describe the remaining work rather than asking to resubmit it.
- Corrected text is published to the subtitle cache after verification. Other
  cached subtitles are preserved/rebound. Cache publication failure is reported
  and queued for recovery without treating a successful remux as unapplied.
- Pending/running repairs are hidden from actionable reports. Following
  completion, subtitle inspection decides whether damage findings remain.
  Language checks are invalidated only for affected subtitles and follow the
  existing schedule; unrelated audio detection is not triggered.

Media API: `POST /api/subtitle-autofix/options`, `POST /prepare`,
`GET /reviews/{id}/progress`, `GET /reviews/{id}/streams/{stream_id}`,
`POST /reviews/{id}/decision`, `POST /reviews/{id}/apply` with `mode=now|queue`,
and `DELETE /reviews/{id}` (paths relative to `/api/subtitle-autofix`).

**UTF-8 quickfix** uses the same review/commit workflow without a saved rule.
It only normalizes verified reversible legacy SRT encoding; it does not change
dialogue or guess lost characters. Its preparation endpoint is
`POST /api/subtitle-autofix/encoding/prepare`. See
[Damage report](SUBTITLE_DAMAGE_REPORT.md#supervised-utf-8-encoding-quickfix)
for eligibility, exclusions and the per-language replacement-word export.

## Verification

- `scripts/test_subtitle_autofix.py`: validation, aliases/regions, Unicode,
  literal/whole-word behavior, deletion, ordering, bounds, CRUD, stale edits,
  corrupt-record retention, bounded-preview timing.
- `scripts/test_subtitle_autofix_postgres.py`: actual PostgreSQL storage and
  concurrent edit protection in a disposable schema; production data untouched.
- `scripts/test_subtitle_autofix_ui.py`: Setup navigation, lazy loading,
  editor actions, draft preservation, save-state reset, errors, and narrow layouts
  using a browser fixture without media/job mutations.
- `scripts/test_subtitle_autofix_media.py`: full-subtitle transformation, cache
  use, per-stream decisions, changed-source guards, bounded reviews, one task,
  repeat-submit protection, targeted refresh and restart/retry receipts.
- `scripts/test_subtitle_autofix_workflow_postgres.py`: actual queue,
  deduplication, workflow locks/LUW, report markers, priorities and output
  receipts in a rollback-only PostgreSQL schema, without production mutations.
- `scripts/test_subtitle_cleanup_batch.py`: real Matroska multi-subtitle repair
  with a single remux, metadata and cache preservation, and injected signature,
  disk, verification and concurrent-change failures against disposable files.
- `scripts/test_subtitle_autofix_review_ui.py`: rule selection, complete safe
  text rendering, individual approval/rejection, queued/immediate replacement,
  persistent errors, operation progress and narrow popup layouts.
