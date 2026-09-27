# Reports

Reports are grouped into Subtitle quality, Language, and Stream configuration.
Movie counts represent media files; TV counts represent episodes, not shows.
Availability is read from indexed data, excludes Final Version media and pending
edits, and respects report-specific language, forced-track, and dismissal rules.
The page shows a checking state until availability is known. A failed check is
not treated as an empty report. Concurrent availability requests are coalesced
in the browser, with a short reuse window while browsing.

Report dialogs share search, refresh, bounded result display (100 entries at a
time), and a scrollable results area. Search only changes the visible list;
bulk actions still apply to the full report. TV reports retain expandable show
trees, including per-episode HTML and image-subtitle entries. Stream Properties
navigation stays within the report, and returning refreshes findings while
restoring the search, expanded shows, and scroll position.

HTML cleanup, language correction, and confidence filters are scoped to their
own report. Responses from a report that has been closed or replaced are
discarded. External subtitles includes media with embedded subtitles as well.

## Regression checks

`python scripts/test_reports_ui.py [base-url]` runs Playwright tests against a
running app with fixture report responses. Mutating API calls are blocked.
Coverage includes action/filter isolation, duplicate movie rendering, damaged
report loading, search/paging, TV trees, report navigation, restoring a report
view, stale responses, and narrow layouts. Live report APIs can also be checked
read-only against `/api/v19/reports/availability`; small count changes are
expected while indexing is actively completing.
