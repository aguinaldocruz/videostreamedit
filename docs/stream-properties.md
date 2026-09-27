# Stream Properties

The shared editor is used from movie/episode lists and reports. Its context
line explicitly distinguishes direct media editing from a TV-show draft.

* Direct editing offers immediate application or queueing.
* TV-show draft editing offers Apply now only: this journals episode changes.
  Saving the whole show remains the real-media commit point.
* Applying keeps the editor open. Queueing reloads committed file values;
  queued proposals are not represented as already-applied file changes.
* Reset edits discards only the unapplied proposal on the current screen.
  Previously applied draft operations remain in the show journal.
* Close/navigation guard unapplied changes. Draft guards hide and reject queue.

The lifecycle coordinator in `app/static/stream-editor.js` serializes editor
loads and guards concurrent submit/navigation. Rendering completes before a
successful operation releases its editor lock. Pending changes are compared
with the current baseline, not suppressed by a permanent clean flag.

Normal loads use the existing fingerprint-validated metadata index. Successful
direct applies explicitly request an authoritative file read. Supporting
change-request status loads concurrently with editor details. Draft projection
responses are checked against the selected media/session before application.

The table retains readable field widths with horizontal scrolling on narrow
windows, a sticky heading, and a wrapping footer outside the scrollable body.

## Regression checks

`python scripts/test_stream_editor_ui.py` uses live app assets but intercepts
every mutation request. It tests immediate apply, queue, draft-only apply,
pending-navigation behavior, subsequent edits and narrow-window scrolling.
It does not modify real media or validate backend media transactions.

Follow with `python scripts/test_reports_ui.py` and
`python scripts/production_smoke.py` for shared report navigation and health.
