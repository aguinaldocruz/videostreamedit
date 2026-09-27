# Job failure safety — September 2026 audit

## Enforced protections

- Media byte sizes and filesystem/Plex timestamps use 64-bit database columns.
  Startup widens existing narrow columns only when necessary; new schemas use
  BIGINT directly. Safety-schema failure prevents task worker startup.
- Explicit retry reopens both the workflow stage and its failed media LUW.
- Completed handlers leave a durable task receipt before completion bookkeeping.
  Retrying a matching output resumes bookkeeping, not the media handler.
- HTML cleanup checkpoints completed subtitle steps before index/Plex bookkeeping.
  A failed full extraction is never accepted as partial replacement text.
  Atomic remuxes record their intended output before rename and acknowledge it
  afterward. Uncertain non-idempotent edits are refused for manual recovery.
- Signatures are checked again after acquiring the media lock. Remuxes also
  verify source identity immediately before replacing the original.
- Snapshot ownership is registered before copying. Interrupted copies remain
  identifiable; retry removes only that incomplete owned copy and rebuilds it.
  A corrupt/missing completed recovery copy prevents further editing.
- Large recovery copies and remux output writes use per-filesystem admission
  locks. Writers on the same filesystem are serialized. In-place metadata
  commands do not use the gate, but any required recovery snapshot does.
  Space is checked after admission, and copying/remuxing stops
  at the configured reserve. Unrelated processes can still consume disk space;
  ENOSPC must always remain a handled failure, not an impossible event.
- Missing OCR executables/unsupported stream families are rejected before
  workflow staging. OCR uses its own approval-stage original, not an additional
  duplicate whole-media workflow copy; originals have checksums and converted
  output identities for supervised rollback.
- Rollback requires an intact original checksum, the recorded failed-output
  identity (including ctime), and an exclusive media LUW lock. It refuses newer
  media, incomplete originals, unknown historical outputs, and multi-media
  rollback batches. Restored media is marked stale for reindexing.
- A task is published completed only after completion bookkeeping. Cleanup
  failure cannot turn a completed edit back into a failed media operation.
- Recovery retention normalizes UUIDs with/without hyphens, so a failed task is
  never mistaken for deleted history. Deleting task history cascades its small
  execution receipts; recovery files follow the existing failed-work policy.
- Identical deduplicated requests are compared without private group/signature
  fields and serialized during admission, preventing concurrent duplicate jobs.
- Abandoned waiting groups with no queue owner, active execution, locks or
  recovery artifacts are retired in bounded batches, not executed again.
  Terminal stage results also repair stale pending group summaries.
- Index priority inheritance compares timestamps as timestamps, including the
  previously failing active “Run sooner” branch.

## Regression coverage

`scripts/test_job_failure_safety.py` uses disposable local files and a
transaction-rolled-back PostgreSQL schema. It covers large values, database
failure after rename, refusal of newer media, HTML bookkeeping-only retry,
interrupted snapshot copying, rollback exclusion, LUW retry and orphan ownership.
Run alongside the consolidation, transaction, retry, retention, startup/index,
and detection-policy regression scripts. No catalog run is required.

## Operational limits

Plex outages, inaccessible mounts, externally replaced files, hardware failure
and language-classification uncertainty cannot be prevented by queue logic.
They must stop safely or be discarded as obsolete. Never infer that an old
failed job is safe to repeat just because its original failure was corrected.
Historical jobs without trustworthy commit evidence require inspection before
reconciliation or rollback. Media LUWs remain granular to one movie/episode;
bulk-job history is not permission to roll back an entire show.

## Verified live reconciliation

Job #12105 failed storing the internal-change marker after HTML cleanup.
The entire requested subtitle (87 cues) was compared against its retained
original: the result exactly matched HTML removal. A verified checkpoint let
the normal worker finish bookkeeping without remuxing again. The task completed
and its no-longer-needed original snapshot was removed. Other failed recovery
originals were retained. No full-catalog processing was requested.
