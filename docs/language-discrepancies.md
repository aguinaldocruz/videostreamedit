# Language discrepancy lifecycle

Final Version media is excluded from subtitle/voice detection admission, deferred
dispatch, warnings and reports. Workers recheck before publishing; audio also
checks between samples. Lightweight core/external-change reconciliation remains
active so genuine replacement or external changes can unfreeze the media.
Finalization retires pending voice/subtitle inspection work, not media-edit jobs.

Manual checks retain evidence. The former reset-media callback is a harmless
refresh acknowledgement for already-open browser tabs, not a destructive reset.
Audio-dot checks target one stream. Subtitle manual checks, scheduled checks and
Evaluate use the same quality/calibration and comparison policy.

Plain Portuguese means unspecified region. Regional mismatches require explicit
conflicting regions; ambiguous regional evidence returns base Portuguese.
The local vocabulary detector currently recognizes Portuguese variants and
English, constrained by the common-language setting. It is not a multilingual
model for every possible language in that list.

Voice warnings require at least two credible agreeing samples, at least 67%
agreement across all requested samples, and a consensus score of at least 60%.
Failed samples count against agreement. The score is a heuristic, not a measured
probability of correctness. Insufficient voice evidence produces no warning.

Subtitle outcomes distinguish agreement, mismatch, insufficient/poor text,
unsupported graphical format, unreadable input and no common-language evidence.
Unsupported graphical subtitles are not treated as damaged text.

Trusted metadata-only language edits can reuse subtitle content evidence and
voice samples for comparison at the scheduled time. Content/structural changes
invalidate the affected family. Track-name/default/forced-only changes do not
request detection. Reuse is not permitted for unrecognized external changes.
Findings join the exact stream family/source/index; missing streams and changed
metadata are not accepted as current findings. Final episodes do not contribute
to TV-show warning summaries.

No full-catalog detection is required to deploy these rules. Existing evidence
can be reconciled without extracting media. New content evidence follows the
configured schedule or an explicit user request.
