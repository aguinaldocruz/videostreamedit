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
* Final Version uses the same Apply/Queue flow as other changes. Once committed,
  the button reads **Final version · set**; a TV draft reads **Final version ·
  draft** until the show is saved. Resetting or refreshing the open editor does
  not remove approval. A genuinely new editing visit retains the existing
  automatic-unfreeze behavior; merely applying and rereading does not count
  as a new visit.

The lifecycle coordinator in `app/static/stream-editor.js` serializes editor
loads and guards concurrent submit/navigation. Rendering completes before a
successful operation releases its editor lock. Pending changes are compared
with the current baseline, not suppressed by a permanent clean flag.

Choosing Queue, Queue changes and move, or Apply now immediately displays the
shared waiting layer. Queue progress covers preparation, server acceptance and
editor refresh/navigation; it does not imply that the background media change
has already finished. Controls stay locked until acknowledgement and refresh
finish. A rejected request unlocks the editor without discarding its proposal.
Read-only browsing and background polling remain non-blocking.

The backend serializes identical queue submissions and returns the existing
pending/running job with an explicit duplicate acknowledgement. Equality requires
both the same complete edit proposal and the same source signature. Different
edits and new source versions remain separate requests, and execution still
refuses unexpectedly changed media. TV-show drafts never create per-episode
media jobs here.

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
Queue cases also assert the waiting layer and step message before submission,
simulate repeated clicks/programmatic submits, and verify rejected queue
requests preserve edits for retry. `test_media_edit_dedup_postgres.py` verifies
one job and one workflow stage for identical pending/running proposals in an
isolated rollback-only database schema.
`python scripts/test_stream_final_version_ui.py` also covers persisted approval
after Apply Now, refresh/reset, explicit unfreezing, new editing visits, queued
approval and draft-only changes. All its mutations are fixture responses.

Follow with `python scripts/test_reports_ui.py` and
`python scripts/production_smoke.py` for shared report navigation and health.
