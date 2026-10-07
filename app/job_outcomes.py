"""Expected terminal job outcomes, distinct from recoverable storage outages."""
from functools import lru_cache
from pathlib import Path
import stat

MISSING_PREFIX = 'Permanent failure [media_missing]:'


class PermanentMediaMissing(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _mount_roots():
    # Decode mountinfo's escaped mount paths (including spaces).
    roots = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        value = line.split()[4]
        for escaped, decoded in (('\\040', ' '), ('\\011', '\t'), ('\\134', '\\')):
            value = value.replace(escaped, decoded)
        roots.append(Path(value))
    return tuple(sorted(roots, key=lambda p: len(p.parts), reverse=True))


def require_media_file(path):
    """Fail once for deleted media; don't classify an unavailable mount as deleted.

    A bind mount's first directory must still be available. Permission, I/O
    errors and missing storage anchors stay ordinary, recoverable errors.
    This is an execution check, not a filesystem scan during queue browsing.
    """
    media = Path(path)
    try:
        info = media.stat()
    except FileNotFoundError as exc:
        root = next((p for p in _mount_roots() if media.is_relative_to(p)), Path('/'))
        relative = media.relative_to(root)
        anchor = root / relative.parts[0] if len(relative.parts) > 1 and root != Path('/') else media.parent
        try:
            if not stat.S_ISDIR(anchor.stat().st_mode):
                raise OSError('Storage anchor is not a directory')
            # Verify directory access, not merely a mountpoint stub's stat.
            next(anchor.iterdir(), None)
        except OSError as storage_exc:
            raise RuntimeError(f'Media storage is unavailable; deletion is not confirmed: {anchor}') from storage_exc
        raise PermanentMediaMissing(
            f'{MISSING_PREFIX} {media}. No automatic retry; returning media requires a new job.'
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise PermanentMediaMissing(f'{MISSING_PREFIX} Not a media file: {media}. Create a new job for a valid file.')
    return info


def finalize_missing_workflow(db, group_id, path, message):
    """Terminate unstarted work for this media only; retain recovery evidence.

    Never fail other media in a bulk workflow or release another owner's lock.
    Caller already holds the workflow mutation lock and fails the current stage.
    """
    if not group_id:
        return
    params = (str(group_id), str(path), message)
    db.execute("""UPDATE task_queue SET status='failed',error=%s,
        progress_message='Permanent failure: media missing',finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
        WHERE replace(group_id,'-','')=replace(%s,'-','') AND status='pending'
        AND COALESCE(payload_json::jsonb#>>'{edit,path}',payload_json::jsonb->>'path',
                     payload_json::jsonb->>'source',payload_json::jsonb->>'media_path')=%s""",
        (message, params[0], params[1]))
    db.execute("""UPDATE index_task_queue SET status='failed',error=%s,finished_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
        WHERE replace(group_id,'-','')=replace(%s,'-','') AND path=%s AND status='pending'""",
        (message, params[0], params[1]))
    db.execute("""UPDATE workflow_stages SET status='failed',error=%s,finished_at=now(),updated_at=now()
        WHERE group_id=%s::uuid AND payload->>'path'=%s AND status IN ('pending','blocked')""",
        (message, params[0], params[1]))
    db.execute("""UPDATE workflow_luws l SET status='failed',error=%s,current_step='media_missing',finished_at=now(),updated_at=now()
        WHERE group_id=%s::uuid AND resource_key=%s AND status IN ('planned','preflighted','waiting')
        AND NOT EXISTS (SELECT 1 FROM workflow_luw_locks k WHERE k.luw_id=l.luw_id)""",
        (message, params[0], params[1]))
    db.execute("""UPDATE workflow_groups g SET status='failed',error=%s,finished_at=now(),updated_at=now()
        WHERE group_id=%s::uuid AND NOT EXISTS
        (SELECT 1 FROM workflow_stages s WHERE s.group_id=g.group_id AND s.status IN ('pending','running','blocked'))""",
        (message, params[0]))
