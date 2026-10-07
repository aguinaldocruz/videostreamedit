# Final Version and reports

Final Version is the user's approval of a media file as it is. Every media
report excludes it, including report availability/counts and Dashboard's
Needs attention findings. Reviewed without Final Version is not an exclusion.

- A final movie is excluded from every movie report.
- A final episode is excluded from every TV report.
- A final TV show excludes all its episodes, even without individual notes.
- Report bulk actions use these eligible lists, so they do not target final media.
- Removing Final Version restores eligibility. Existing findings are retained;
  no full-catalog scan is needed to hide or restore them.
- Existing external-change handling can remove approval when the actual media
  changes; the normal indexing/inspection rules then determine fresh findings.

All report lists and counts share `report_blocked_paths()` and the committed
`final_paths()` rule. Report/UI caches are invalidated after successful approval
changes or media edits, including when a count request was already in flight.
Matroska report revalidation filters out final media before accessing files.
Draft-only TV edits do not change committed report eligibility until saved.

Regression checks:

- `scripts/test_final_version_reports.py`: real queries in a disposable
  PostgreSQL schema; all movie/TV report types, voice findings, counts, show
  inheritance, reviewed-only media, unfreezing and existing pending-job hiding.
- `scripts/test_final_version_report_ui.py`: browser checks for approval-change
  notifications, report list/count refresh and delayed-response races.
