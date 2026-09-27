# Collection dashboard

The dashboard is read-only: navigation never starts indexing or media edits.

* Compact Movies / TV Shows cards retain storage and exact indexed counts.
  Index existence is explicitly distinguished from live file verification.
* Review states are mutually exclusive: Final, reviewed-not-final, unreviewed.
  TV progress counts episodes; fully finalized shows have a separate count.
* Review/change counts open a paginated, exact-scope media list. Its editor
  navigation stays within the loaded page, and closing returns to that list.
* Background work separates media tasks, indexes and preflight validation.
  Status/type buttons navigate to explicit filters, not destructive actions.
  Global workload remains global when the collection scope changes.
* Backlog trend compares successive samples in the current browser session.
  Recent throughput covers retained successful jobs in the last hour. There
  is no misleading mixed-job ETA or overload warning based on queue length.
* Findings respect report visibility and open the matching report. Counts
  overlap and are not presented as a unique total of affected media.
* Continue reviewing lists up to eight partially finalized shows.
* Languages and libraries load only when Collection insights is expanded.
  Language regions and external subtitles are included.

The overview is database-only, cached for 30 seconds, with concurrent refresh
coalescing. The browser refreshes every 30 seconds only when visible and no
dialog is open. Report counts refresh at most once per minute automatically.
Refresh retains old content while fetching; failures leave navigation usable.
Scope persists locally; returning preserves scroll position and insights state.

Regression: `python scripts/test_dashboard_ui.py` uses mocked summaries and
blocks mutation requests. Actual edits, queue execution and resource-pressure
diagnostics remain in their existing dedicated screens.
