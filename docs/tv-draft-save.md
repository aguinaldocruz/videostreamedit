# TV-show draft submission

Draft edits are persisted as an ordered database journal before Save. Save submits a small journal reference to one `tv_edit_session_commit` task; it does not inspect, remux, or modify episode files in the HTTP request. Repeated Save requests return the same submitted task.

The interface shows a submission splash until the queue acknowledges the request, then a clickable job number with queued/running status and available progress. Browsing other shows is allowed. Returning or refreshing recovers the draft status from the server. A failed status poll never unlocks the show.

Submitted shows reject unrelated media and note changes while committing. The owning commit worker is permitted to execute its journal. The worker consolidates changes per episode, verifies the captured media fingerprint, and uses the existing per-media write protections. New journal-reference jobs retire successfully processed journal entries; failed entries remain available for review.

No new copy of every episode is created at submission. Older already-queued draft tasks retain their original payload and remain supported while they finish.

Tests: `scripts/test_tv_draft_handoff.py` (isolated persistence/queue) and `scripts/test_tv_draft_handoff_ui.py` (browser, mocked writes).
